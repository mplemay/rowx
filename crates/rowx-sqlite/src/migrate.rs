use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Instant;

use rowx_core::migrate::{
    AppliedMigration, Migration, MigrationStatus, PlanError, PlanOptions, RevertTarget, SchemaName,
    SourceError, TableName, details, failed_message, plain, plan_revert, plan_run, plan_skip,
    planned,
};
pub use rowx_core::migrate::{AppliedRecord, MigrationDetail};
use rusqlite::Connection as Raw;
use thiserror::Error;

use crate::conn::Connection;
use crate::error::Error;
use crate::exec::{Param, interrupted};
use crate::timeout::Timer;

// the `execution_time` of a row whose time is not known (a skipped migration, or one still running)
const UNKNOWN_EXECUTION_TIME: i64 = -1;
// the only schema SQLite has besides `temp`, which a history table cannot live in
const MAIN: &str = "main";

const AUTH_MESSAGE: &str = "a migration must not contain BEGIN, COMMIT or ROLLBACK: rowx runs each file \
    in a transaction of its own, or in none when the file is marked `-- no-transaction`";
const OPEN_TRANSACTION_MESSAGE: &str = "a `-- no-transaction` migration must not leave a transaction open (an \
    unreleased SAVEPOINT); the work done since that SAVEPOINT was rolled back";
const FOREIGN_KEYS_MESSAGE: &str = "foreign key enforcement could not be turned back on after a \
    `-- no-transaction` migration";
// how many violating rows a failure names; the rest are counted
const VIOLATIONS_SHOWN: usize = 10;
const BUSY_MESSAGE: &str = "another migrator holds the database: the write lock was not released \
    within the busy timeout (`connect_timeout`)";

#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum ConfigError {
    #[error(
        "the migration table {table:?} is in the schema {schema:?}, but SQLite has no schemas \
         besides `main`; use a plain table name"
    )]
    Schema { table: String, schema: String },
    #[error("`create_schemas` is not supported on SQLite, which has no schemas; remove it")]
    CreateSchemas,
}

#[derive(Debug, Error)]
pub enum MigrateError {
    #[error(transparent)]
    Source(#[from] SourceError),
    #[error(transparent)]
    Plan(#[from] PlanError),
    // the file (or its commit) failed; `source` keeps the SQLite error
    #[error("{}", failed_message(*version, false, source))]
    Failed {
        version: i64,
        #[source]
        source: Error,
        // the history row was left with `success = 0`
        dirty: bool,
        // the status of each migration applied before this one failed
        applied: Box<[MigrationDetail]>,
    },
    // `BEGIN IMMEDIATE` waited for the busy timeout and the other writer never let go
    #[error("{BUSY_MESSAGE}")]
    Busy(#[source] Error),
    #[error(transparent)]
    Connection(#[from] Error),
    // a `-- no-transaction` file claimed the version and its row still has no time: the migrator running it
    // may still be at work, or it was interrupted before it could record the outcome
    #[error(
        "migration {version} is dirty: a `-- no-transaction` migration claimed it and has not finished; \
         another migrator may still be running it, so wait for that one to end; if none is running, it \
         was interrupted: fix the database and its history row"
    )]
    Claimed { version: i64 },
    // the caller went away, so no further step was started
    #[error("the migration was cancelled")]
    Cancelled,
}

impl MigrateError {
    /// The SQLite error behind this one, for a failure that has one.
    #[must_use]
    pub const fn driver(&self) -> Option<&Error> {
        match self {
            Self::Failed { source: e, .. } | Self::Busy(e) | Self::Connection(e) => Some(e),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MigrateConfig {
    table: TableName,
}

impl MigrateConfig {
    /// # Errors
    /// `Schema` for a table in a schema other than `main`, `CreateSchemas` for any schema to create.
    pub fn new(table: TableName, create_schemas: &[SchemaName]) -> Result<Self, ConfigError> {
        if !create_schemas.is_empty() {
            return Err(ConfigError::CreateSchemas);
        }
        match table.schema() {
            Some(schema) if schema != MAIN => Err(ConfigError::Schema {
                table: table.name().to_owned(),
                schema: schema.to_owned(),
            }),
            _ => Ok(Self { table }),
        }
    }

    #[must_use]
    pub const fn table(&self) -> &TableName {
        &self.table
    }
}

// A dirty row whose time is still unknown is a claim a `-- no-transaction` file made: a migrator that finished
// it, or saw it fail, wrote the time.
fn plan_failure(applied: &[AppliedRecord]) -> impl Fn(PlanError) -> MigrateError + '_ {
    move |err| match err {
        PlanError::Dirty { version }
            if applied.iter().any(|r| {
                r.migration.version == version
                    && !r.migration.success
                    && r.execution_time == UNKNOWN_EXECUTION_TIME
            }) =>
        {
            MigrateError::Claimed { version }
        }
        other => other.into(),
    }
}

fn stop(cancelled: &AtomicBool) -> Result<(), MigrateError> {
    if cancelled.load(Ordering::Acquire) {
        return Err(MigrateError::Cancelled);
    }
    Ok(())
}

fn nanos(started: Instant) -> i64 {
    i64::try_from(started.elapsed().as_nanos()).unwrap_or(i64::MAX)
}

// the history table as the SQL spells it, and as the catalog stores it
#[derive(Debug, Clone)]
struct History {
    quoted: String,
    name: String,
}

// Every reference names the `main` schema: an unqualified name resolves to a TEMP table of the same name
// first, which a file can create, and the history would then be read from and written to that table.
impl From<&MigrateConfig> for History {
    fn from(config: &MigrateConfig) -> Self {
        let quoted = config.table.quoted();
        Self {
            quoted: if config.table.schema().is_some() {
                quoted
            } else {
                format!("\"{MAIN}\".{quoted}")
            },
            name: config.table.name().to_owned(),
        }
    }
}

// the columns of sqlx's SQLite history table, so a table sqlx created is read as it is
fn create_sql(quoted: &str) -> String {
    format!(
        "CREATE TABLE IF NOT EXISTS {quoted} (\n\
         version BIGINT PRIMARY KEY,\n\
         description TEXT NOT NULL,\n\
         installed_on TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,\n\
         success BOOLEAN NOT NULL,\n\
         checksum BLOB NOT NULL,\n\
         execution_time BIGINT NOT NULL\n\
         )"
    )
}

fn read_records(raw: &Raw, quoted: &str) -> Result<Vec<AppliedRecord>, Error> {
    let mut stmt = raw.prepare(&format!(
        "SELECT version, checksum, success, \
         CAST(strftime('%s', installed_on) AS INTEGER) * 1000000, execution_time \
         FROM {quoted} ORDER BY version"
    ))?;
    let rows = stmt.query_map([], |row| {
        Ok(AppliedRecord {
            migration: AppliedMigration {
                version: row.get(0)?,
                checksum: row.get(1)?,
                success: row.get(2)?,
            },
            installed_on_micros: row.get::<_, Option<i64>>(3)?.unwrap_or_default(),
            execution_time: row.get(4)?,
        })
    })?;
    Ok(rows.collect::<Result<Vec<_>, _>>()?)
}

fn table_exists(raw: &Raw, name: &str) -> Result<bool, Error> {
    Ok(raw.query_row(
        "SELECT EXISTS(SELECT 1 FROM main.sqlite_master \
         WHERE type = 'table' AND lower(name) = lower(?1))",
        [name],
        |row| row.get(0),
    )?)
}

/// The history rows by version; no rows when the table doesn't exist, so nothing is created.
///
/// # Errors
/// `Connection` for a failure SQLite reports.
pub async fn list_applied(
    conn: &Connection,
    config: &MigrateConfig,
) -> Result<Vec<AppliedRecord>, MigrateError> {
    let History { quoted, name } = History::from(config);
    Ok(conn
        .call(move |ctx| {
            if ctx.shared.is_cancelled() {
                return Err(interrupted());
            }
            // the statement timeout bounds the read, as it bounds any other statement
            let _armed = ctx.timer.as_ref().map(Timer::arm);
            if table_exists(&ctx.conn, &name)? {
                read_records(&ctx.conn, &quoted)
            } else {
                Ok(Vec::new())
            }
        })
        .await?)
}

// the write lock, waited for up to the busy timeout
async fn begin(conn: &Connection) -> Result<(), MigrateError> {
    conn.simple("BEGIN IMMEDIATE").await.map_err(|err| {
        if err.is_busy() {
            MigrateError::Busy(err)
        } else {
            err.into()
        }
    })
}

// Ends a transaction nobody will finish. A rollback that keeps failing marks the connection broken, which
// is how the caller learns of it.
async fn abandon(conn: &Connection) {
    let _ = conn.rollback_abandoned().await;
}

// the file's own `BEGIN`, `COMMIT` and `ROLLBACK` are refused by the driver before they run
fn script_error(err: Error) -> Error {
    match err {
        Error::Sqlite { code, .. } if code & 0xff == rusqlite::ffi::SQLITE_AUTH => {
            Error::from_code(code, AUTH_MESSAGE, None)
        }
        other => other,
    }
}

async fn script(conn: &Connection, sql: &str) -> Result<(), Error> {
    conn.execute_migration_script(sql)
        .await
        .map_err(script_error)
}

// `foreign_keys` is per connection and a `-- no-transaction` file may have turned it off to rebuild a table;
// the driver turned it on at connect
async fn restore_foreign_keys(conn: &Connection) -> Result<(), Error> {
    conn.simple_uncancelled("PRAGMA foreign_keys = ON").await?;
    let on = conn
        .call(|ctx| {
            Ok(ctx
                .conn
                .query_row("PRAGMA foreign_keys", [], |row| row.get::<_, i64>(0))?)
        })
        .await?;
    if on == 1 {
        return Ok(());
    }
    Err(Error::from_code(
        rusqlite::ffi::SQLITE_ERROR,
        FOREIGN_KEYS_MESSAGE,
        None,
    ))
}

fn violations_message(found: &[String], total: usize) -> String {
    let more = total.saturating_sub(found.len());
    let tail = if more > 0 {
        format!("; and {more} more")
    } else {
        String::new()
    };
    format!(
        "after the `-- no-transaction` migration, {total} row(s) violate a foreign key \
         (`PRAGMA foreign_key_check`), so the migration is left dirty: {}{tail}",
        found.join("; ")
    )
}

// What SQLite's own table-rebuild procedure ends with. A file may turn enforcement off (and back on) to rebuild
// a table, and rows it left without a parent would go unnoticed: the file cannot say whether it did, so the
// check runs after every `-- no-transaction` file
async fn check_foreign_keys(conn: &Connection) -> Result<(), Error> {
    let (found, total) = conn
        .call(|ctx| {
            if ctx.shared.is_cancelled() {
                return Err(interrupted());
            }
            let mut stmt = ctx.conn.prepare("PRAGMA foreign_key_check")?;
            // the scan covers the whole database, so the statement timeout bounds it as it bounds the file's
            // own statements; the clock starts after the prepare, where an interrupt would be lost
            let _armed = ctx.timer.as_ref().map(Timer::arm);
            let mut rows = stmt.query([])?;
            let mut found = Vec::new();
            let mut total = 0_usize;
            while let Some(row) = rows.next()? {
                total += 1;
                if found.len() < VIOLATIONS_SHOWN {
                    let table: String = row.get(0)?;
                    let rowid: Option<i64> = row.get(1)?;
                    let parent: String = row.get(2)?;
                    let row_name =
                        rowid.map_or_else(|| "a row".to_owned(), |id| format!("rowid {id}"));
                    found.push(format!(
                        "{table} {row_name} references a missing row of {parent}"
                    ));
                }
            }
            Ok((found, total))
        })
        .await?;
    if total == 0 {
        return Ok(());
    }
    Err(Error::from_code(
        rusqlite::ffi::SQLITE_CONSTRAINT_FOREIGNKEY,
        violations_message(&found, total),
        None,
    ))
}

// What a `-- no-transaction` file leaves behind. A file that left a transaction open (an unreleased
// `SAVEPOINT` starts one in autocommit mode) has it rolled back, because the history write would otherwise
// land in a transaction nobody commits and `PRAGMA foreign_keys` would silently do nothing inside it.
async fn after_script(conn: &Connection, ran: Result<(), Error>) -> Result<(), Error> {
    let mut outcome = ran;
    if !conn.is_autocommit().await? {
        conn.rollback_abandoned().await?;
        if outcome.is_ok() {
            outcome = Err(Error::from_code(
                rusqlite::ffi::SQLITE_ERROR,
                OPEN_TRANSACTION_MESSAGE,
                None,
            ));
        }
    }
    let restored = restore_foreign_keys(conn).await;
    outcome.and(restored)?;
    check_foreign_keys(conn).await
}

// The time a failed `-- no-transaction` file ran, so its dirty row no longer reads as a claim still at work.
// Best effort: the failure being reported matters more than this write.
async fn record_failure(conn: &Connection, quoted: &str, version: i64, started: Instant) {
    let _ = execute(
        conn,
        format!("UPDATE {quoted} SET execution_time = ?1 WHERE version = ?2 AND success = 0"),
        vec![Param::Integer(nanos(started)), Param::Integer(version)],
    )
    .await;
}

fn failure(version: i64, dirty: bool, source: Error) -> MigrateError {
    MigrateError::Failed {
        version,
        source,
        dirty,
        applied: Box::default(),
    }
}

fn failed(version: i64) -> impl Fn(Error) -> MigrateError {
    move |source| failure(version, false, source)
}

// a failure after the row was written with `success = 0`
fn failed_dirty(version: i64) -> impl Fn(Error) -> MigrateError {
    move |source| failure(version, true, source)
}

async fn execute(conn: &Connection, sql: String, params: Vec<Param>) -> Result<(), Error> {
    conn.execute(&sql, params).await?;
    Ok(())
}

async fn insert_row(
    conn: &Connection,
    quoted: &str,
    migration: &Migration,
    success: bool,
    execution_time: i64,
) -> Result<(), Error> {
    execute(
        conn,
        format!(
            "INSERT INTO {quoted} (version, description, success, checksum, execution_time) \
             VALUES (?1, ?2, ?3, ?4, ?5)"
        ),
        vec![
            Param::Integer(migration.version),
            Param::Text(migration.description.clone()),
            Param::Integer(i64::from(success)),
            Param::Blob(migration.checksum.to_vec()),
            Param::Integer(execution_time),
        ],
    )
    .await
}

async fn set_success(
    conn: &Connection,
    quoted: &str,
    version: i64,
    success: bool,
    execution_time: Option<i64>,
) -> Result<(), Error> {
    execute(
        conn,
        format!(
            "UPDATE {quoted} SET success = ?1, execution_time = coalesce(?2, execution_time) \
             WHERE version = ?3"
        ),
        vec![
            Param::Integer(i64::from(success)),
            execution_time.map_or(Param::Null, Param::Integer),
            Param::Integer(version),
        ],
    )
    .await
}

async fn delete_row(conn: &Connection, quoted: &str, version: i64) -> Result<(), Error> {
    execute(
        conn,
        format!("DELETE FROM {quoted} WHERE version = ?1"),
        vec![Param::Integer(version)],
    )
    .await
}

/// `CREATE TABLE IF NOT EXISTS` for the history table, in a transaction of its own and only when the table
/// is missing, so an existing one is never written to.
///
/// # Errors
/// `Busy` when another migrator holds the database, `Connection` for a failure SQLite reports.
pub async fn ensure_table(conn: &Connection, config: &MigrateConfig) -> Result<(), MigrateError> {
    let history = History::from(config);
    let name = history.name.clone();
    let exists = conn.call(move |ctx| table_exists(&ctx.conn, &name)).await?;
    if exists {
        return Ok(());
    }
    begin(conn).await?;
    // IF NOT EXISTS: another migrator may have created it while this one waited for the lock
    let made = async {
        conn.simple(&create_sql(&history.quoted)).await?;
        conn.simple("COMMIT").await
    }
    .await;
    if let Err(err) = made {
        abandon(conn).await;
        return Err(err.into());
    }
    Ok(())
}

// The status of each version the operation touched, in the order it touched them
async fn touched(
    conn: &Connection,
    config: &MigrateConfig,
    local: &[Migration],
    versions: &[i64],
) -> Result<Vec<MigrationDetail>, MigrateError> {
    let after = details(local, &list_applied(conn, config).await?);
    Ok(versions
        .iter()
        .filter_map(|version| after.iter().find(|d| d.info.version == *version).cloned())
        .collect())
}

// Adds what the earlier steps did to a step's failure
async fn with_applied(
    conn: &Connection,
    config: &MigrateConfig,
    local: &[Migration],
    done: &[i64],
    // how long each completed step took, when the history row no longer says (a revert)
    elapsed: Option<&[i64]>,
    err: MigrateError,
) -> MigrateError {
    let MigrateError::Failed {
        version,
        source,
        dirty,
        ..
    } = err
    else {
        return err;
    };
    let mut applied = touched(conn, config, local, done).await.unwrap_or_default();
    if let Some(elapsed) = elapsed {
        for (detail, nanos) in applied.iter_mut().zip(elapsed) {
            detail.execution_time = Some(*nanos);
        }
    }
    MigrateError::Failed {
        version,
        source,
        dirty,
        applied: applied.into_boxed_slice(),
    }
}

// Applies the first pending migration, under the write lock and from the applied set read under it, so
// another migrator's progress is seen. `None` when nothing is pending any more.
async fn apply_next(
    conn: &Connection,
    config: &MigrateConfig,
    local: &[Migration],
    options: PlanOptions,
) -> Result<Option<i64>, MigrateError> {
    let History { quoted, .. } = History::from(config);
    begin(conn).await?;
    let started = Instant::now();
    let step = match list_applied(conn, config).await.and_then(|applied| {
        plan_run(local, &plain(&applied), options)
            .map(|plan| plan.steps.into_iter().next())
            .map_err(plan_failure(&applied))
    }) {
        Ok(Some(step)) => step,
        Ok(None) => {
            abandon(conn).await;
            return Ok(None);
        }
        Err(err) => {
            abandon(conn).await;
            return Err(err);
        }
    };
    let version = step.version;
    if step.no_transaction {
        // the version is claimed by a committed row before the file runs, so a crash leaves it dirty
        let claimed = async {
            insert_row(conn, &quoted, &step, false, UNKNOWN_EXECUTION_TIME)
                .await
                .map_err(failed(version))?;
            conn.simple("COMMIT").await.map_err(failed(version))
        }
        .await;
        if let Err(err) = claimed {
            abandon(conn).await;
            return Err(err);
        }
        let ran = script(conn, &step.sql).await;
        let finished = match after_script(conn, ran).await {
            Ok(()) => set_success(conn, &quoted, version, true, Some(nanos(started))).await,
            Err(err) => Err(err),
        };
        if let Err(err) = finished {
            record_failure(conn, &quoted, version, started).await;
            return Err(failed_dirty(version)(err));
        }
        return Ok(Some(version));
    }
    let applied = async {
        script(conn, &step.sql).await.map_err(failed(version))?;
        // the time is written with the row, so a migrator that is waiting for the lock is never made to wait
        // for a second write after this one committed
        insert_row(conn, &quoted, &step, true, nanos(started))
            .await
            .map_err(failed(version))?;
        conn.simple("COMMIT").await.map_err(failed(version))
    }
    .await;
    if let Err(err) = applied {
        abandon(conn).await;
        return Err(err);
    }
    Ok(Some(version))
}

// Reverts one version of a plan under the write lock. `false` when another migrator reverted it first.
async fn revert_one(
    conn: &Connection,
    config: &MigrateConfig,
    step: &Migration,
) -> Result<bool, MigrateError> {
    let History { quoted, .. } = History::from(config);
    let version = step.version;
    begin(conn).await?;
    let row = match list_applied(conn, config).await {
        Ok(applied) => applied.into_iter().find(|r| r.migration.version == version),
        Err(err) => {
            abandon(conn).await;
            return Err(err);
        }
    };
    match row {
        None => {
            abandon(conn).await;
            return Ok(false);
        }
        Some(row) if !row.migration.success => {
            abandon(conn).await;
            return Err(plan_failure(std::slice::from_ref(&row))(PlanError::Dirty {
                version,
            }));
        }
        Some(_) => {}
    }
    if step.no_transaction {
        let started = Instant::now();
        // the unknown time marks the row as a claim, as a run's claim row is
        let claimed = async {
            set_success(conn, &quoted, version, false, Some(UNKNOWN_EXECUTION_TIME))
                .await
                .map_err(failed(version))?;
            conn.simple("COMMIT").await.map_err(failed(version))
        }
        .await;
        if let Err(err) = claimed {
            abandon(conn).await;
            return Err(err);
        }
        let ran = script(conn, &step.sql).await;
        let finished = match after_script(conn, ran).await {
            Ok(()) => delete_row(conn, &quoted, version).await,
            Err(err) => Err(err),
        };
        if let Err(err) = finished {
            record_failure(conn, &quoted, version, started).await;
            return Err(failed_dirty(version)(err));
        }
        return Ok(true);
    }
    let reverted = async {
        script(conn, &step.sql).await.map_err(failed(version))?;
        delete_row(conn, &quoted, version)
            .await
            .map_err(failed(version))?;
        conn.simple("COMMIT").await.map_err(failed(version))
    }
    .await;
    if let Err(err) = reverted {
        abandon(conn).await;
        return Err(err);
    }
    Ok(true)
}

/// Applies every pending migration the plan selects, each in a `BEGIN IMMEDIATE` transaction of its own that
/// commits with its history row, and returns the status of each, in order.
///
/// # Errors
/// `Plan` before anything runs, `Failed` for a file that fails, `Busy` when another migrator holds the
/// database for longer than the busy timeout, `Connection` otherwise.
pub async fn run(
    conn: &Connection,
    config: &MigrateConfig,
    local: &[Migration],
    options: PlanOptions,
    cancelled: &AtomicBool,
) -> Result<Vec<MigrationDetail>, MigrateError> {
    ensure_table(conn, config).await?;
    let applied = list_applied(conn, config).await?;
    // read without the lock, so a run with nothing to do never waits for one
    if plan_run(local, &plain(&applied), options)
        .map_err(plan_failure(&applied))?
        .is_empty()
    {
        return Ok(Vec::new());
    }
    let mut done = Vec::new();
    loop {
        stop(cancelled)?;
        match apply_next(conn, config, local, options).await {
            Ok(Some(version)) => done.push(version),
            Ok(None) => break,
            Err(err) => return Err(with_applied(conn, config, local, &done, None, err).await),
        }
    }
    touched(conn, config, local, &done).await
}

/// Reverts the applied versions `target` selects, newest first, and returns the status of each.
///
/// Each status's `execution_time` is how long its revert took.
///
/// # Errors
/// As [`run`]; `Plan(MissingDown)` is raised before anything is reverted.
pub async fn revert(
    conn: &Connection,
    config: &MigrateConfig,
    local: &[Migration],
    target: RevertTarget,
    ignore_missing: bool,
    cancelled: &AtomicBool,
) -> Result<Vec<MigrationDetail>, MigrateError> {
    ensure_table(conn, config).await?;
    let applied = list_applied(conn, config).await?;
    // `Latest` is resolved once, here, so it never reaches past the version it named
    let plan = plan_revert(local, &plain(&applied), target, ignore_missing)
        .map_err(plan_failure(&applied))?;
    let mut done = Vec::with_capacity(plan.steps.len());
    let mut elapsed = Vec::with_capacity(plan.steps.len());
    for step in &plan.steps {
        stop(cancelled)?;
        let started = Instant::now();
        match revert_one(conn, config, step).await {
            Ok(true) => {
                done.push(step.version);
                elapsed.push(nanos(started));
            }
            Ok(false) => {}
            Err(err) => {
                return Err(with_applied(conn, config, local, &done, Some(&elapsed), err).await);
            }
        }
    }
    // a reverted version has no history row, so its execution_time is how long the revert took
    let mut details = touched(conn, config, local, &done).await?;
    for (detail, nanos) in details.iter_mut().zip(elapsed) {
        detail.execution_time = Some(nanos);
    }
    Ok(details)
}

/// Records the pending migrations as applied without running them, all in one transaction, and returns the
/// status of each.
///
/// # Errors
/// As [`run`].
pub async fn skip(
    conn: &Connection,
    config: &MigrateConfig,
    local: &[Migration],
    options: PlanOptions,
    cancelled: &AtomicBool,
) -> Result<Vec<MigrationDetail>, MigrateError> {
    ensure_table(conn, config).await?;
    let applied = list_applied(conn, config).await?;
    if plan_skip(local, &plain(&applied), options)
        .map_err(plan_failure(&applied))?
        .is_empty()
    {
        return Ok(Vec::new());
    }
    stop(cancelled)?;
    let History { quoted, .. } = History::from(config);
    begin(conn).await?;
    let recorded = async {
        // the plan is made again from what is committed now
        let applied = list_applied(conn, config).await?;
        let plan = plan_skip(local, &plain(&applied), options).map_err(plan_failure(&applied))?;
        let mut done = Vec::with_capacity(plan.steps.len());
        for step in &plan.steps {
            insert_row(conn, &quoted, step, true, UNKNOWN_EXECUTION_TIME).await?;
            done.push(step.version);
        }
        conn.simple("COMMIT").await?;
        Ok::<_, MigrateError>(done)
    }
    .await;
    match recorded {
        Ok(done) => touched(conn, config, local, &done).await,
        Err(err) => {
            abandon(conn).await;
            Err(err)
        }
    }
}

/// The statuses [`run`] would return, from the same plan, without running anything or taking the write
/// lock. Creates nothing: a missing history table makes everything pending.
///
/// # Errors
/// The `Plan` errors [`run`] raises and `Connection`.
pub async fn run_dry(
    conn: &Connection,
    config: &MigrateConfig,
    local: &[Migration],
    options: PlanOptions,
) -> Result<Vec<MigrationDetail>, MigrateError> {
    let applied = list_applied(conn, config).await?;
    let plan = plan_run(local, &plain(&applied), options).map_err(plan_failure(&applied))?;
    Ok(planned(&plan.steps, MigrationStatus::Applied))
}

/// The statuses [`revert`] would return (with no `execution_time`), without reverting anything or taking
/// the write lock. Creates nothing.
///
/// # Errors
/// The `Plan` errors [`revert`] raises and `Connection`.
pub async fn revert_dry(
    conn: &Connection,
    config: &MigrateConfig,
    local: &[Migration],
    target: RevertTarget,
    ignore_missing: bool,
) -> Result<Vec<MigrationDetail>, MigrateError> {
    let applied = list_applied(conn, config).await?;
    let plan = plan_revert(local, &plain(&applied), target, ignore_missing)
        .map_err(plan_failure(&applied))?;
    Ok(planned(&plan.steps, MigrationStatus::Pending))
}

/// The statuses [`skip`] would return, from the same plan, without recording anything or taking the write
/// lock. Creates nothing.
///
/// # Errors
/// The `Plan` errors [`skip`] raises and `Connection`.
pub async fn skip_dry(
    conn: &Connection,
    config: &MigrateConfig,
    local: &[Migration],
    options: PlanOptions,
) -> Result<Vec<MigrationDetail>, MigrateError> {
    let applied = list_applied(conn, config).await?;
    let plan = plan_skip(local, &plain(&applied), options).map_err(plan_failure(&applied))?;
    Ok(planned(&plan.steps, MigrationStatus::Applied))
}

/// The status of every version. Takes no write lock and creates nothing: a missing table makes everything
/// pending.
///
/// # Errors
/// `Connection` for a failure SQLite reports.
pub async fn info(
    conn: &Connection,
    config: &MigrateConfig,
    local: &[Migration],
) -> Result<Vec<MigrationDetail>, MigrateError> {
    Ok(details(local, &list_applied(conn, config).await?))
}

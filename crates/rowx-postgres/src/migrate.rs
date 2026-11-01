use std::time::Instant;

use rowx_core::migrate::{
    AppliedMigration, Migration, MigrationInfo, MigrationStatus, PlanError, PlanOptions,
    RevertTarget, SchemaName, SourceError, TableName, migration_info, plan_run, plan_skip,
    plan_undo,
};
use thiserror::Error;

use crate::conn::Connection;
use crate::error::Error;

// sqlx's multiplier, so a sqlx migrator and a rowx migrator exclude each other
const LOCK_MULTIPLIER: i64 = 0x3d32_ad9e;
// the `execution_time` of a row whose time is not known yet (sqlx #1966)
const UNKNOWN_EXECUTION_TIME: i64 = -1;

#[derive(Debug, Error)]
pub enum MigrateError {
    #[error(transparent)]
    Source(#[from] SourceError),
    #[error(transparent)]
    Plan(#[from] PlanError),
    // the file (or its commit) failed; `source` keeps the server error with its sqlstate and position
    #[error("migration {version} failed: {source}")]
    Failed {
        version: i64,
        #[source]
        source: Error,
    },
    #[error(transparent)]
    Connection(#[from] Error),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MigrateConfig {
    pub table: TableName,
    pub create_schemas: Vec<SchemaName>,
    pub locking: bool,
}

impl MigrateConfig {
    #[must_use]
    pub const fn new(table: TableName) -> Self {
        Self {
            table,
            create_schemas: Vec::new(),
            locking: true,
        }
    }
}

// An applied row with the columns `info` reports
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AppliedRecord {
    pub migration: AppliedMigration,
    pub installed_on_micros: i64,
    pub execution_time: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MigrationDetail {
    pub info: MigrationInfo,
    // `Some` for versions that have a history row
    pub installed_on_micros: Option<i64>,
    pub execution_time: Option<i64>,
}

/// sqlx's advisory-lock key for a database name.
#[must_use]
pub fn lock_key(database: &str) -> i64 {
    LOCK_MULTIPLIER * i64::from(crc32fast::hash(database.as_bytes()))
}

/// `CREATE SCHEMA IF NOT EXISTS` for each schema, then `CREATE TABLE IF NOT EXISTS`.
///
/// # Errors
/// `Connection` for a server error.
pub async fn ensure_table(
    conn: &Connection,
    table: &TableName,
    create_schemas: &[SchemaName],
) -> Result<(), MigrateError> {
    let quoted = table.quoted();
    for schema in create_schemas {
        conn.simple(&format!("CREATE SCHEMA IF NOT EXISTS {}", schema.quoted()))
            .await?;
    }
    conn.simple(&format!(
        "CREATE TABLE IF NOT EXISTS {quoted} (\n\
         version BIGINT PRIMARY KEY,\n\
         description TEXT NOT NULL,\n\
         installed_on TIMESTAMPTZ NOT NULL DEFAULT now(),\n\
         success BOOLEAN NOT NULL,\n\
         checksum BYTEA NOT NULL,\n\
         execution_time BIGINT NOT NULL\n\
         )"
    ))
    .await?;
    Ok(())
}

/// The history rows by version; no rows when the table doesn't exist, so nothing is created.
///
/// # Errors
/// `Connection` for a server error.
pub async fn list_applied(
    conn: &Connection,
    table: &TableName,
) -> Result<Vec<AppliedRecord>, MigrateError> {
    let quoted = table.quoted();
    let exists: bool = conn
        .client()?
        .query_one(
            "SELECT pg_catalog.to_regclass($1::text) IS NOT NULL",
            &[&quoted],
        )
        .await
        .map_err(Error::from)?
        .get(0);
    if !exists {
        return Ok(Vec::new());
    }
    list_records(conn, &quoted).await
}

// the rows of a history table that exists, by its quoted name
async fn list_records(conn: &Connection, quoted: &str) -> Result<Vec<AppliedRecord>, MigrateError> {
    let rows = conn
        .client()?
        .query(
            &format!(
                "SELECT version, checksum, success, \
                 (EXTRACT(EPOCH FROM installed_on) * 1000000)::bigint, execution_time \
                 FROM {quoted} ORDER BY version"
            ),
            &[],
        )
        .await
        .map_err(Error::from)?;
    Ok(rows
        .iter()
        .map(|row| AppliedRecord {
            migration: AppliedMigration {
                version: row.get(0),
                checksum: row.get(1),
                success: row.get(2),
            },
            installed_on_micros: row.get(3),
            execution_time: row.get(4),
        })
        .collect())
}

fn details(local: &[Migration], applied: &[AppliedRecord]) -> Vec<MigrationDetail> {
    migration_info(local, &plain(applied))
        .into_iter()
        .map(|info| {
            let record = applied.iter().find(|r| r.migration.version == info.version);
            MigrationDetail {
                installed_on_micros: record.map(|r| r.installed_on_micros),
                execution_time: record.map(|r| r.execution_time),
                info,
            }
        })
        .collect()
}

// The status of each version the operation touched, in the order it touched them. It is read under
// the same lock as the change, from the table resolved before the files ran.
async fn touched(
    conn: &Connection,
    quoted: &str,
    local: &[Migration],
    versions: &[i64],
) -> Result<Vec<MigrationDetail>, MigrateError> {
    let after = details(local, &list_records(conn, quoted).await?);
    Ok(versions
        .iter()
        .filter_map(|version| after.iter().find(|d| d.info.version == *version).cloned())
        .collect())
}

fn plain(records: &[AppliedRecord]) -> Vec<AppliedMigration> {
    records.iter().map(|r| r.migration.clone()).collect()
}

fn nanos(started: Instant) -> i64 {
    i64::try_from(started.elapsed().as_nanos()).unwrap_or(i64::MAX)
}

async fn insert_row(
    conn: &Connection,
    quoted: &str,
    migration: &Migration,
    success: bool,
) -> Result<(), Error> {
    conn.client()?
        .execute(
            &format!(
                "INSERT INTO {quoted} (version, description, success, checksum, execution_time) \
                 VALUES ($1, $2, $3, $4, $5)"
            ),
            &[
                &migration.version,
                &migration.description,
                &success,
                &migration.checksum.as_slice(),
                &UNKNOWN_EXECUTION_TIME,
            ],
        )
        .await?;
    Ok(())
}

async fn execute(conn: &Connection, sql: &str, version: i64) -> Result<(), Error> {
    conn.client()?.execute(sql, &[&version]).await?;
    Ok(())
}

fn failed(version: i64) -> impl Fn(Error) -> MigrateError {
    move |source| MigrateError::Failed { version, source }
}

async fn apply(conn: &Connection, quoted: &str, migration: &Migration) -> Result<(), MigrateError> {
    let version = migration.version;
    let started = Instant::now();
    let finish = |success_too: bool| {
        let set = if success_too {
            "success = TRUE, execution_time"
        } else {
            "execution_time"
        };
        format!("UPDATE {quoted} SET {set} = $1 WHERE version = $2")
    };
    if migration.no_transaction {
        insert_row(conn, quoted, migration, false).await?;
        conn.simple(&migration.sql).await.map_err(failed(version))?;
        conn.client()?
            .execute(&finish(true), &[&nanos(started), &version])
            .await
            .map_err(Error::from)?;
        return Ok(());
    }
    conn.simple("BEGIN").await?;
    conn.simple(&migration.sql).await.map_err(failed(version))?;
    insert_row(conn, quoted, migration, true).await?;
    conn.simple("COMMIT").await.map_err(failed(version))?;
    // outside the transaction, because the elapsed time includes the commit (sqlx #1966)
    conn.client()?
        .execute(&finish(false), &[&nanos(started), &version])
        .await
        .map_err(Error::from)?;
    Ok(())
}

async fn revert(
    conn: &Connection,
    quoted: &str,
    migration: &Migration,
) -> Result<(), MigrateError> {
    let version = migration.version;
    let delete = format!("DELETE FROM {quoted} WHERE version = $1");
    if migration.no_transaction {
        execute(
            conn,
            &format!("UPDATE {quoted} SET success = FALSE WHERE version = $1"),
            version,
        )
        .await?;
        conn.simple(&migration.sql).await.map_err(failed(version))?;
        execute(conn, &delete, version).await?;
        return Ok(());
    }
    conn.simple("BEGIN").await?;
    conn.simple(&migration.sql).await.map_err(failed(version))?;
    execute(conn, &delete, version).await?;
    conn.simple("COMMIT").await.map_err(failed(version))?;
    Ok(())
}

// Takes the lock (unless `locking` is off), runs `body`, and releases the lock whatever happened. On
// an error the session may sit in an aborted transaction, so it is rolled back first (a warning and a
// no-op otherwise) whether or not the lock is used; the rollback and the unlock are best effort then,
// and a pool's reset is the backstop.
async fn locked<T>(
    conn: &Connection,
    config: &MigrateConfig,
    body: impl AsyncFnOnce() -> Result<T, MigrateError>,
) -> Result<T, MigrateError> {
    let key = if config.locking {
        let client = conn.client()?;
        let database: String = client
            .query_one("SELECT pg_catalog.current_database()", &[])
            .await
            .map_err(Error::from)?
            .get(0);
        let key = lock_key(&database);
        client
            .query("SELECT pg_catalog.pg_advisory_lock($1)", &[&key])
            .await
            .map_err(Error::from)?;
        Some(key)
    } else {
        None
    };
    let result = body().await;
    if result.is_err() {
        let _ = conn.simple("ROLLBACK").await;
    }
    let Some(key) = key else {
        return result;
    };
    let unlocked = match conn.client() {
        Ok(client) => client
            .query("SELECT pg_catalog.pg_advisory_unlock($1)", &[&key])
            .await
            .map(|_| ())
            .map_err(Error::from),
        Err(e) => Err(e),
    };
    match (result, unlocked) {
        (Ok(value), Ok(())) => Ok(value),
        (Ok(_), Err(e)) => Err(e.into()),
        (Err(e), _) => Err(e),
    }
}

// The existing history table as a schema-qualified, quoted name. A file may change `search_path`
// (`SET LOCAL` in its transaction, or `SET` for the session), so the bookkeeping after it must not
// resolve an unqualified name again.
async fn resolve_table(conn: &Connection, table: &TableName) -> Result<String, MigrateError> {
    let quoted = table.quoted();
    let row = conn
        .client()?
        .query_one(
            "SELECT pg_catalog.format('%I.%I', n.nspname, c.relname) \
             FROM pg_catalog.pg_class c \
             JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace \
             WHERE c.oid = pg_catalog.to_regclass($1::text)",
            &[&quoted],
        )
        .await
        .map_err(Error::from)?;
    Ok(row.get(0))
}

/// Applies every pending migration the plan selects and returns the status of each, in order.
///
/// # Errors
/// `Plan` before anything runs, `Failed` for a file that fails, `Connection` otherwise.
pub async fn run(
    conn: &Connection,
    config: &MigrateConfig,
    local: &[Migration],
    options: PlanOptions,
) -> Result<Vec<MigrationDetail>, MigrateError> {
    locked(conn, config, async || {
        ensure_table(conn, &config.table, &config.create_schemas).await?;
        let quoted = resolve_table(conn, &config.table).await?;
        let applied = list_applied(conn, &config.table).await?;
        let plan = plan_run(local, &plain(&applied), options)?;
        let mut done = Vec::with_capacity(plan.steps.len());
        for step in &plan.steps {
            apply(conn, &quoted, step).await?;
            done.push(step.version);
        }
        touched(conn, &quoted, local, &done).await
    })
    .await
}

/// Reverts the applied versions `target` selects, newest first, and returns the status of each.
///
/// Each status's `execution_time` is how long its revert took.
///
/// # Errors
/// As [`run`]; `Plan(MissingDown)` is raised before anything is reverted.
pub async fn undo(
    conn: &Connection,
    config: &MigrateConfig,
    local: &[Migration],
    target: RevertTarget,
    ignore_missing: bool,
) -> Result<Vec<MigrationDetail>, MigrateError> {
    locked(conn, config, async || {
        ensure_table(conn, &config.table, &config.create_schemas).await?;
        let quoted = resolve_table(conn, &config.table).await?;
        let applied = list_applied(conn, &config.table).await?;
        let applied = plain(&applied);
        // `Latest` is resolved by the plan, under the same lock as the revert
        let plan = plan_undo(local, &applied, target, ignore_missing)?;
        let mut done = Vec::with_capacity(plan.steps.len());
        let mut elapsed = Vec::with_capacity(plan.steps.len());
        for step in &plan.steps {
            let started = Instant::now();
            revert(conn, &quoted, step).await?;
            done.push(step.version);
            elapsed.push(nanos(started));
        }
        // a reverted version has no history row, so its execution_time is how long the revert took
        let mut details = touched(conn, &quoted, local, &done).await?;
        for (detail, nanos) in details.iter_mut().zip(elapsed) {
            detail.execution_time = Some(nanos);
        }
        Ok(details)
    })
    .await
}

// The statuses a plan's steps would have afterwards, for a dry run
fn planned(steps: &[Migration], status: MigrationStatus) -> Vec<MigrationDetail> {
    steps
        .iter()
        .map(|step| MigrationDetail {
            info: MigrationInfo {
                version: step.version,
                description: Some(step.description.clone()),
                status,
            },
            installed_on_micros: None,
            execution_time: None,
        })
        .collect()
}

/// The statuses [`run`] would return, from the same plan under the same lock, without running
/// anything. Creates nothing: a missing history table makes everything pending.
///
/// # Errors
/// The `Plan` errors [`run`] raises and `Connection`.
pub async fn run_dry(
    conn: &Connection,
    config: &MigrateConfig,
    local: &[Migration],
    options: PlanOptions,
) -> Result<Vec<MigrationDetail>, MigrateError> {
    locked(conn, config, async || {
        let applied = list_applied(conn, &config.table).await?;
        let plan = plan_run(local, &plain(&applied), options)?;
        Ok(planned(&plan.steps, MigrationStatus::Applied))
    })
    .await
}

/// The statuses [`undo`] would return (with no `execution_time`), without reverting anything.
/// Creates nothing.
///
/// # Errors
/// The `Plan` errors [`undo`] raises and `Connection`.
pub async fn undo_dry(
    conn: &Connection,
    config: &MigrateConfig,
    local: &[Migration],
    target: RevertTarget,
    ignore_missing: bool,
) -> Result<Vec<MigrationDetail>, MigrateError> {
    locked(conn, config, async || {
        let applied = list_applied(conn, &config.table).await?;
        let applied = plain(&applied);
        // `Latest` is resolved by the plan, under the same lock as the revert
        let plan = plan_undo(local, &applied, target, ignore_missing)?;
        Ok(planned(&plan.steps, MigrationStatus::Pending))
    })
    .await
}

/// Records the pending migrations as applied without running them, and returns the status of each.
///
/// # Errors
/// As [`run`].
pub async fn skip(
    conn: &Connection,
    config: &MigrateConfig,
    local: &[Migration],
    options: PlanOptions,
) -> Result<Vec<MigrationDetail>, MigrateError> {
    locked(conn, config, async || {
        ensure_table(conn, &config.table, &config.create_schemas).await?;
        let quoted = resolve_table(conn, &config.table).await?;
        let applied = list_applied(conn, &config.table).await?;
        let plan = plan_skip(local, &plain(&applied), options)?;
        let mut done = Vec::with_capacity(plan.steps.len());
        for step in &plan.steps {
            insert_row(conn, &quoted, step, true).await?;
            done.push(step.version);
        }
        touched(conn, &quoted, local, &done).await
    })
    .await
}

/// The status of every version. Creates nothing: a missing table makes everything pending.
///
/// # Errors
/// `Connection` for a server error.
pub async fn info(
    conn: &Connection,
    config: &MigrateConfig,
    local: &[Migration],
) -> Result<Vec<MigrationDetail>, MigrateError> {
    locked(conn, config, async || {
        Ok(details(local, &list_applied(conn, &config.table).await?))
    })
    .await
}

#[cfg(test)]
mod tests {
    use super::lock_key;

    #[test]
    fn lock_key_matches_sqlx() {
        // 0x3d32ad9e * crc32(name), computed independently with zlib.crc32
        assert_eq!(lock_key("postgres"), 368_907_709_894_077_238);
        assert_eq!(lock_key("rowx"), 2_710_434_580_532_335_694);
    }
}

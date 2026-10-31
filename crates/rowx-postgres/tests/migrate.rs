// pub, so the shared helpers are reachable from the crate root and need no visibility waiver
pub mod common;

use std::collections::hash_map::RandomState;
use std::hash::{BuildHasher, Hasher};
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use common::TestResult;
use rowx_core::migrate::{
    Migration, MigrationKind, MigrationStatus, PlanError, PlanOptions, checksum,
};
use rowx_postgres::admin::{create_database, drop_database};
use rowx_postgres::cancel::{Hook, spawn};
use rowx_postgres::conn::Connection;
use rowx_postgres::dsn::ConnectOptions;
use rowx_postgres::error::Error;
use rowx_postgres::migrate::{
    MigrateConfig, MigrateError, info, list_applied, lock_key, run, skip, undo,
};
use rowx_postgres::pool::{PgManager, Pool, PoolOptions, RESET_QUERY};
use tokio::runtime::Handle;

const TABLE: &str = "_rowx_migrations";

// a database of its own, so the advisory-lock key is private to the test
struct TestDb {
    base: ConnectOptions,
    name: String,
    options: ConnectOptions,
}

impl TestDb {
    async fn new() -> TestResult<Option<Self>> {
        let Some(dsn) = common::test_dsn()? else {
            return Ok(None);
        };
        let base = ConnectOptions::from_dsn(&dsn)?;
        let name = format!(
            "rowx_mig_{:016x}",
            RandomState::new().build_hasher().finish()
        );
        create_database(&base, &name, None, &Handle::current()).await?;
        let options = ConnectOptions {
            database: Some(name.clone()),
            ..base.clone()
        };
        Ok(Some(Self {
            base,
            name,
            options,
        }))
    }

    async fn connect(&self) -> Result<Connection, Error> {
        Connection::connect(&self.options, &Handle::current()).await
    }

    async fn finish(self) -> TestResult {
        drop_database(&self.base, &self.name, true, &Handle::current()).await?;
        Ok(())
    }
}

fn migration(version: i64, kind: MigrationKind, sql: &str) -> Migration {
    Migration {
        version,
        description: format!("m{version}"),
        kind,
        sql: sql.to_owned(),
        checksum: checksum(sql.as_bytes()),
        no_transaction: sql.starts_with("-- no-transaction\n"),
        path: PathBuf::new(),
    }
}

fn simple(version: i64, sql: &str) -> Migration {
    migration(version, MigrationKind::Simple, sql)
}

fn reversible(version: i64, up: &str, down: &str) -> [Migration; 2] {
    [
        migration(version, MigrationKind::Up, up),
        migration(version, MigrationKind::Down, down),
    ]
}

fn config() -> MigrateConfig {
    MigrateConfig::new(TABLE)
}

fn target(version: i64) -> PlanOptions {
    PlanOptions {
        target: Some(version),
        ..PlanOptions::default()
    }
}

async fn count(conn: &Connection, sql: &str) -> TestResult<i64> {
    Ok(conn.client()?.query_one(sql, &[]).await?.get(0))
}

// (version, success) of every history row
async fn history(conn: &Connection, table: &str) -> TestResult<Vec<(i64, bool)>> {
    let rows = conn
        .client()?
        .query(
            &format!("SELECT version, success FROM {table} ORDER BY version"),
            &[],
        )
        .await?;
    Ok(rows.iter().map(|r| (r.get(0), r.get(1))).collect())
}

fn sqlstate(err: &MigrateError) -> Option<&str> {
    match err {
        MigrateError::Failed {
            source: Error::Db { sqlstate, .. },
            ..
        } => Some(sqlstate),
        _ => None,
    }
}

async fn advisory_locks(conn: &Connection) -> TestResult<i64> {
    count(
        conn,
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND database = \
         (SELECT oid FROM pg_database WHERE datname = current_database())",
    )
    .await
}

#[tokio::test]
async fn applies_reapplies_and_stops_at_a_target() -> TestResult {
    let Some(db) = TestDb::new().await? else {
        return Ok(());
    };
    let mut conn = db.connect().await?;
    let all = [
        simple(1, "CREATE TABLE a (i int)"),
        simple(2, "CREATE TABLE b (i int)"),
        simple(3, "CREATE TABLE c (i int)"),
    ];

    let first = run(&conn, &config(), &all, target(2)).await?;
    assert_eq!(first, [1, 2]);
    assert_eq!(history(&conn, TABLE).await?, [(1, true), (2, true)]);

    let rest = run(&conn, &config(), &all, PlanOptions::default()).await?;
    assert_eq!(rest, [3]);
    let again = run(&conn, &config(), &all, PlanOptions::default()).await?;
    assert_eq!(again.len(), 0);
    assert_eq!(history(&conn, TABLE).await?.len(), 3);

    // the description and the up checksum are recorded, and execution_time replaces the -1
    let row = conn
        .client()?
        .query_one(
            &format!("SELECT description, checksum, execution_time FROM {TABLE} WHERE version = 2"),
            &[],
        )
        .await?;
    assert_eq!(row.get::<_, String>(0), "m2");
    assert_eq!(row.get::<_, Vec<u8>>(1), all[1].checksum.to_vec());
    assert!(row.get::<_, i64>(2) >= 0);

    conn.close().await;
    db.finish().await
}

#[tokio::test]
async fn a_failing_transactional_migration_leaves_nothing() -> TestResult {
    let Some(db) = TestDb::new().await? else {
        return Ok(());
    };
    let mut conn = db.connect().await?;
    let all = [
        simple(1, "CREATE TABLE ok (i int)"),
        simple(2, "CREATE TABLE partial (i int); SELECT 1/0"),
    ];

    let err = run(&conn, &config(), &all, PlanOptions::default()).await;
    let err = err.err().ok_or("expected a failure")?;
    assert!(
        matches!(err, MigrateError::Failed { version: 2, .. }),
        "{err:?}"
    );
    assert_eq!(sqlstate(&err), Some("22012"));

    assert_eq!(history(&conn, TABLE).await?, [(1, true)]);
    assert_eq!(
        count(
            &conn,
            "SELECT count(*) FROM pg_tables WHERE tablename = 'partial'"
        )
        .await?,
        0
    );
    // the lock was released and the connection is usable and out of the aborted transaction
    assert_eq!(advisory_locks(&conn).await?, 0);
    conn.close().await;
    db.finish().await
}

#[tokio::test]
async fn the_server_error_keeps_its_position() -> TestResult {
    let Some(db) = TestDb::new().await? else {
        return Ok(());
    };
    let mut conn = db.connect().await?;
    let err = run(
        &conn,
        &config(),
        &[simple(1, "SELECT 1;\nSELEC 2")],
        PlanOptions::default(),
    )
    .await
    .err()
    .ok_or("expected a failure")?;
    match err {
        MigrateError::Failed {
            version: 1,
            source: Error::Db {
                sqlstate, position, ..
            },
        } => {
            assert_eq!(&*sqlstate, "42601");
            assert!(position.is_some());
        }
        other => return Err(format!("unexpected {other:?}").into()),
    }
    conn.close().await;
    db.finish().await
}

#[tokio::test]
async fn a_failing_no_transaction_migration_is_dirty_and_blocks_the_next_run() -> TestResult {
    let Some(db) = TestDb::new().await? else {
        return Ok(());
    };
    let mut conn = db.connect().await?;
    let all = [
        simple(1, "-- no-transaction\nCREATE TABLE t (i int); SELECT 1/0"),
        simple(2, "CREATE TABLE after (i int)"),
    ];

    let err = run(&conn, &config(), &all, PlanOptions::default())
        .await
        .err()
        .ok_or("expected a failure")?;
    assert_eq!(sqlstate(&err), Some("22012"));
    assert_eq!(history(&conn, TABLE).await?, [(1, false)]);
    // the file ran as one implicit transaction, so Postgres rolled it back, but the row is dirty anyway
    assert_eq!(
        count(
            &conn,
            "SELECT count(*) FROM pg_tables WHERE tablename = 't'"
        )
        .await?,
        0
    );

    let next = run(&conn, &config(), &all, PlanOptions::default())
        .await
        .err()
        .ok_or("expected the dirty row to block")?;
    assert!(
        matches!(next, MigrateError::Plan(PlanError::Dirty { version: 1 })),
        "{next:?}"
    );
    assert_eq!(advisory_locks(&conn).await?, 0);

    let details = info(&conn, &config(), &all).await?;
    assert_eq!(details[0].info.status, MigrationStatus::Dirty);
    conn.close().await;
    db.finish().await
}

#[tokio::test]
async fn create_index_concurrently_works_only_as_the_sole_statement() -> TestResult {
    let Some(db) = TestDb::new().await? else {
        return Ok(());
    };
    let mut conn = db.connect().await?;
    let setup = [simple(1, "CREATE TABLE users (email text)")];
    run(&conn, &config(), &setup, PlanOptions::default()).await?;

    let good = [
        setup[0].clone(),
        simple(
            2,
            "-- no-transaction\nCREATE INDEX CONCURRENTLY users_email ON users (email);",
        ),
    ];
    assert_eq!(
        run(&conn, &config(), &good, PlanOptions::default()).await?,
        [2]
    );
    assert_eq!(
        count(
            &conn,
            "SELECT count(*) FROM pg_indexes WHERE indexname = 'users_email'"
        )
        .await?,
        1
    );
    assert_eq!(history(&conn, TABLE).await?, [(1, true), (2, true)]);

    // another statement in the same Query makes it a transaction block, which Postgres refuses
    let bad = [
        good[0].clone(),
        good[1].clone(),
        simple(
            3,
            "-- no-transaction\nSELECT 1; CREATE INDEX CONCURRENTLY users_email2 ON users (email);",
        ),
    ];
    let err = run(&conn, &config(), &bad, PlanOptions::default())
        .await
        .err()
        .ok_or("expected a failure")?;
    assert_eq!(sqlstate(&err), Some("25001"));
    assert_eq!(history(&conn, TABLE).await?[2], (3, false));
    conn.close().await;
    db.finish().await
}

#[tokio::test]
async fn a_no_transaction_file_can_commit_between_enum_steps() -> TestResult {
    let Some(db) = TestDb::new().await? else {
        return Ok(());
    };
    let mut conn = db.connect().await?;
    let all = [
        simple(1, "CREATE TYPE mood AS ENUM ('sad')"),
        simple(
            2,
            "-- no-transaction\nBEGIN; ALTER TYPE mood ADD VALUE 'happy'; COMMIT;\n\
             BEGIN; CREATE TABLE t (m mood DEFAULT 'happy'); COMMIT;",
        ),
    ];
    assert_eq!(
        run(&conn, &config(), &all, PlanOptions::default()).await?,
        [1, 2]
    );
    assert_eq!(history(&conn, TABLE).await?, [(1, true), (2, true)]);
    conn.close().await;
    db.finish().await
}

#[tokio::test]
async fn undo_reverts_newest_first_to_a_target_and_to_zero() -> TestResult {
    let Some(db) = TestDb::new().await? else {
        return Ok(());
    };
    let mut conn = db.connect().await?;
    let mut all = Vec::new();
    for (v, t) in [(1, "a"), (2, "b"), (3, "c")] {
        all.extend(reversible(
            v,
            &format!("CREATE TABLE {t} (i int)"),
            &format!("DROP TABLE {t}"),
        ));
    }
    run(&conn, &config(), &all, PlanOptions::default()).await?;

    assert_eq!(undo(&conn, &config(), &all, 1, false).await?, [3, 2]);
    assert_eq!(history(&conn, TABLE).await?, [(1, true)]);
    assert_eq!(
        count(
            &conn,
            "SELECT count(*) FROM pg_tables WHERE tablename IN ('b', 'c')"
        )
        .await?,
        0
    );

    assert_eq!(undo(&conn, &config(), &all, 0, false).await?, [1]);
    assert_eq!(history(&conn, TABLE).await?.len(), 0);
    assert_eq!(undo(&conn, &config(), &all, 0, false).await?.len(), 0);
    conn.close().await;
    db.finish().await
}

#[tokio::test]
async fn undo_without_a_down_file_reverts_nothing() -> TestResult {
    let Some(db) = TestDb::new().await? else {
        return Ok(());
    };
    let mut conn = db.connect().await?;
    let mut all = reversible(1, "CREATE TABLE a (i int)", "DROP TABLE a").to_vec();
    all.push(simple(2, "CREATE TABLE b (i int)"));
    run(&conn, &config(), &all, PlanOptions::default()).await?;

    let err = undo(&conn, &config(), &all, 0, false)
        .await
        .err()
        .ok_or("expected a failure")?;
    assert!(
        matches!(
            err,
            MigrateError::Plan(PlanError::MissingDown { version: 2 })
        ),
        "{err:?}"
    );
    assert_eq!(history(&conn, TABLE).await?, [(1, true), (2, true)]);
    assert_eq!(advisory_locks(&conn).await?, 0);
    conn.close().await;
    db.finish().await
}

#[tokio::test]
async fn a_failing_transactional_revert_keeps_the_row_and_the_schema() -> TestResult {
    let Some(db) = TestDb::new().await? else {
        return Ok(());
    };
    let mut conn = db.connect().await?;
    let all = reversible(1, "CREATE TABLE a (i int)", "DROP TABLE a; SELECT 1/0");
    run(&conn, &config(), &all, PlanOptions::default()).await?;

    let err = undo(&conn, &config(), &all, 0, false)
        .await
        .err()
        .ok_or("expected a failure")?;
    assert_eq!(sqlstate(&err), Some("22012"));
    assert_eq!(history(&conn, TABLE).await?, [(1, true)]);
    assert_eq!(
        count(
            &conn,
            "SELECT count(*) FROM pg_tables WHERE tablename = 'a'"
        )
        .await?,
        1
    );
    conn.close().await;
    db.finish().await
}

#[tokio::test]
async fn a_failing_no_transaction_revert_leaves_a_dirty_row() -> TestResult {
    let Some(db) = TestDb::new().await? else {
        return Ok(());
    };
    let mut conn = db.connect().await?;
    let all = reversible(
        1,
        "CREATE TABLE a (i int)",
        "-- no-transaction\nDROP TABLE nope",
    );
    run(&conn, &config(), &all, PlanOptions::default()).await?;
    let err = undo(&conn, &config(), &all, 0, false)
        .await
        .err()
        .ok_or("expected a failure")?;
    assert_eq!(sqlstate(&err), Some("42P01"));
    assert_eq!(history(&conn, TABLE).await?, [(1, false)]);

    // a successful no-transaction revert deletes the row
    let ok = reversible(
        2,
        "CREATE TABLE b (i int)",
        "-- no-transaction\nDROP TABLE b",
    );
    run(&conn, &config(), &ok, PlanOptions::default())
        .await
        .err()
        .ok_or("the dirty row must block run")?;
    conn.close().await;
    db.finish().await
}

#[tokio::test]
async fn a_no_transaction_revert_deletes_the_row() -> TestResult {
    let Some(db) = TestDb::new().await? else {
        return Ok(());
    };
    let mut conn = db.connect().await?;
    let all = reversible(
        1,
        "CREATE TABLE a (i int)",
        "-- no-transaction\nDROP TABLE a",
    );
    run(&conn, &config(), &all, PlanOptions::default()).await?;
    assert_eq!(undo(&conn, &config(), &all, 0, false).await?, [1]);
    assert_eq!(history(&conn, TABLE).await?.len(), 0);
    conn.close().await;
    db.finish().await
}

#[tokio::test]
async fn skip_records_without_running() -> TestResult {
    let Some(db) = TestDb::new().await? else {
        return Ok(());
    };
    let mut conn = db.connect().await?;
    let all = [
        simple(1, "CREATE TABLE a (i int)"),
        simple(2, "CREATE TABLE b (i int)"),
    ];
    assert_eq!(skip(&conn, &config(), &all, target(1)).await?, [1]);
    assert_eq!(history(&conn, TABLE).await?, [(1, true)]);
    assert_eq!(
        count(
            &conn,
            "SELECT count(*) FROM pg_tables WHERE tablename = 'a'"
        )
        .await?,
        0
    );
    // the skipped one counts as applied
    assert_eq!(
        run(&conn, &config(), &all, PlanOptions::default()).await?,
        [2]
    );
    let unknown = count(
        &conn,
        &format!("SELECT execution_time FROM {TABLE} WHERE version = 1"),
    )
    .await?;
    assert_eq!(unknown, -1);
    conn.close().await;
    db.finish().await
}

#[tokio::test]
async fn a_schema_qualified_table_and_create_schemas() -> TestResult {
    let Some(db) = TestDb::new().await? else {
        return Ok(());
    };
    let mut conn = db.connect().await?;
    let cfg = MigrateConfig {
        table: "my app.history".to_owned(),
        create_schemas: vec!["my app".to_owned(), "other".to_owned()],
        locking: true,
    };
    run(
        &conn,
        &cfg,
        &[simple(1, "SELECT 1")],
        PlanOptions::default(),
    )
    .await?;
    assert_eq!(history(&conn, "\"my app\".\"history\"").await?, [(1, true)]);
    assert_eq!(
        count(
            &conn,
            "SELECT count(*) FROM pg_namespace WHERE nspname IN ('my app', 'other')"
        )
        .await?,
        2
    );
    assert_eq!(list_applied(&conn, "my app.history").await?.len(), 1);

    // a missing schema without create_schemas is the server's error, not a silent creation
    let bare = MigrateConfig::new("absent.history");
    let err = run(&conn, &bare, &[], PlanOptions::default())
        .await
        .err()
        .ok_or("expected a failure")?;
    assert!(
        matches!(err, MigrateError::Connection(Error::Db { .. })),
        "{err:?}"
    );

    for bad in ["a.b.c", "", "a."] {
        let err = run(&conn, &MigrateConfig::new(bad), &[], PlanOptions::default()).await;
        assert!(matches!(err, Err(MigrateError::InvalidTable(_))), "{bad:?}");
    }
    conn.close().await;
    db.finish().await
}

#[tokio::test]
async fn info_creates_nothing_and_reports_every_state() -> TestResult {
    let Some(db) = TestDb::new().await? else {
        return Ok(());
    };
    let mut conn = db.connect().await?;
    let cfg = MigrateConfig {
        create_schemas: Vec::new(),
        ..config()
    };
    let all = [simple(1, "SELECT 1"), simple(2, "SELECT 2")];

    let before = info(&conn, &cfg, &all).await?;
    assert!(
        before
            .iter()
            .all(|d| d.info.status == MigrationStatus::Pending)
    );
    assert!(before.iter().all(|d| d.execution_time.is_none()));
    assert_eq!(
        count(
            &conn,
            &format!("SELECT count(*) FROM pg_tables WHERE tablename = '{TABLE}'")
        )
        .await?,
        0
    );
    assert_eq!(list_applied(&conn, TABLE).await?.len(), 0);

    run(&conn, &cfg, &all[..1], PlanOptions::default()).await?;
    let after = info(&conn, &cfg, &all).await?;
    assert_eq!(after[0].info.status, MigrationStatus::Applied);
    assert!(after[0].installed_on_micros.is_some_and(|t| t > 0));
    assert!(after[0].execution_time.is_some_and(|t| t >= 0));
    assert_eq!(after[1].info.status, MigrationStatus::Pending);

    let modified = [simple(1, "SELECT 'changed'")];
    let changed = info(&conn, &cfg, &modified).await?;
    assert_eq!(changed[0].info.status, MigrationStatus::Modified);
    let err = run(&conn, &cfg, &modified, PlanOptions::default()).await;
    assert!(matches!(
        err,
        Err(MigrateError::Plan(PlanError::Checksum { version: 1 }))
    ));
    let missing = info(&conn, &cfg, &[]).await?;
    assert_eq!(missing[0].info.status, MigrationStatus::Missing);
    conn.close().await;
    db.finish().await
}

#[tokio::test]
async fn reads_a_table_and_checksums_written_by_sqlx() -> TestResult {
    let Some(db) = TestDb::new().await? else {
        return Ok(());
    };
    let mut conn = db.connect().await?;
    // sqlx's DDL, verbatim, and a row as sqlx records it: the SHA-384 of the file bytes
    conn.simple(
        "CREATE TABLE IF NOT EXISTS _sqlx_migrations (
            version BIGINT PRIMARY KEY,
            description TEXT NOT NULL,
            installed_on TIMESTAMPTZ NOT NULL DEFAULT now(),
            success BOOLEAN NOT NULL,
            checksum BYTEA NOT NULL,
            execution_time BIGINT NOT NULL
        );
        INSERT INTO _sqlx_migrations (version, description, success, checksum, execution_time)
        VALUES (20240101000000, 'create users', TRUE,
            decode('24454095d8d7876ca8e842f34296ca645c83e0f7b50f048c99db4b2ff4da659267afc4ef3e994f3d74e18c741165d931', 'hex'),
            12345);",
    )
    .await?;
    let cfg = MigrateConfig::new("_sqlx_migrations");

    let sqlx_file = Migration {
        description: "create users".to_owned(),
        ..simple(20_240_101_000_000, "SELECT 1;\n")
    };
    let next = simple(20_240_102_000_000, "CREATE TABLE next (i int)");
    let all = [sqlx_file.clone(), next];

    let details = info(&conn, &cfg, &all).await?;
    assert_eq!(details[0].info.status, MigrationStatus::Applied);
    assert_eq!(details[0].execution_time, Some(12345));
    assert_eq!(
        run(&conn, &cfg, &all, PlanOptions::default()).await?,
        [20_240_102_000_000]
    );

    // a different file for the same version is a modified migration, not a pending one
    let edited = Migration {
        sql: "SELECT 2;\n".to_owned(),
        checksum: checksum(b"SELECT 2;\n"),
        ..sqlx_file
    };
    let err = run(
        &conn,
        &cfg,
        &[edited, all[1].clone()],
        PlanOptions::default(),
    )
    .await;
    assert!(matches!(
        err,
        Err(MigrateError::Plan(PlanError::Checksum {
            version: 20_240_101_000_000
        }))
    ));
    conn.close().await;
    db.finish().await
}

// polls pg_locks from `conn` until `waiters` advisory-lock requests wait in this database
async fn wait_for_lock_waiters(conn: &Connection, waiters: i64) -> TestResult {
    tokio::time::timeout(Duration::from_secs(30), async {
        loop {
            let waiting = count(
                conn,
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted AND \
                 database = (SELECT oid FROM pg_database WHERE datname = current_database())",
            )
            .await;
            if waiting.is_ok_and(|n| n >= waiters) {
                return;
            }
            tokio::task::yield_now().await;
        }
    })
    .await?;
    Ok(())
}

#[tokio::test]
async fn concurrent_runs_wait_on_the_lock_then_find_nothing_to_do() -> TestResult {
    let Some(db) = TestDb::new().await? else {
        return Ok(());
    };
    let mut holder = db.connect().await?;
    let key = lock_key(&db.name);
    holder
        .client()?
        .query("SELECT pg_advisory_lock($1)", &[&key])
        .await?;

    let all = Arc::new([simple(1, "CREATE TABLE once (i int)")]);
    let mut tasks = Vec::new();
    for _ in 0..2 {
        let conn = db.connect().await?;
        let all = Arc::clone(&all);
        tasks.push(tokio::spawn(async move {
            let result = run(&conn, &config(), &*all, PlanOptions::default())
                .await
                .map_err(|e| e.to_string());
            (conn, result)
        }));
    }
    wait_for_lock_waiters(&holder, 2).await?;
    // nothing ran while the lock was held elsewhere
    assert_eq!(
        count(
            &holder,
            "SELECT count(*) FROM pg_tables WHERE tablename = 'once'"
        )
        .await?,
        0
    );
    holder
        .client()?
        .query("SELECT pg_advisory_unlock($1)", &[&key])
        .await?;

    let mut applied = Vec::new();
    for task in tasks {
        let (mut conn, result) = task.await?;
        applied.push(result?);
        conn.close().await;
    }
    applied.sort();
    assert_eq!(applied, [Vec::<i64>::new(), vec![1]]);
    assert_eq!(history(&holder, TABLE).await?, [(1, true)]);
    assert_eq!(advisory_locks(&holder).await?, 0);

    // locking = false never touches the lock, even while another session holds it
    holder
        .client()?
        .query("SELECT pg_advisory_lock($1)", &[&key])
        .await?;
    let free = db.connect().await?;
    let unlocked = MigrateConfig {
        locking: false,
        ..config()
    };
    let more = [
        simple(1, "CREATE TABLE once (i int)"),
        simple(2, "SELECT 2"),
    ];
    assert_eq!(
        run(&free, &unlocked, &more, PlanOptions::default()).await?,
        [2]
    );
    holder.close().await;
    let mut free = free;
    free.close().await;
    db.finish().await
}

fn pool_for(db: &TestDb) -> TestResult<Pool> {
    let manager = PgManager::new(&db.options, Handle::current(), Some(RESET_QUERY.to_owned()))?;
    let options = PoolOptions {
        max_size: 1,
        acquire_timeout: Duration::from_secs(30),
        ..PoolOptions::default()
    };
    Ok(Pool::new(manager, options, Handle::current())?)
}

// runs a migration whose file sleeps, drops the future once the sleep has started, and returns the
// history rows left behind after the pool has settled and reset the connection
async fn cancelled_run(db: &TestDb, file: &str) -> TestResult<(Vec<(i64, bool)>, i64)> {
    let pool = pool_for(db)?;
    let mut observer = db.connect().await?;
    let pooled = pool.acquire().await?;
    let conn = pooled.conn()?;
    let claim = conn
        .cancel
        .state
        .claim_settled_leased(pooled.lease())
        .await?;
    let inner = Arc::clone(&conn.cancel.backend.conn);
    let local = vec![simple(1, file)];
    let hook = Hook::default();
    let pending = spawn(&conn.cancel, claim, &hook, async move {
        let guard = inner.lock().await;
        run(&guard, &config(), &local, PlanOptions::default())
            .await
            .map(|_| ())
    });

    tokio::time::timeout(Duration::from_secs(30), async {
        loop {
            let sleeping = count(
                &observer,
                "SELECT count(*) FROM pg_stat_activity WHERE query LIKE '%pg_sleep(600)%' \
                 AND pid <> pg_backend_pid() AND datname = current_database()",
            )
            .await;
            if sleeping.is_ok_and(|n| n > 0) {
                return;
            }
            tokio::task::yield_now().await;
        }
    })
    .await?;
    drop(pending);
    drop(pooled);

    // with max_size = 1 this waits until the cancelled task has ended and the connection was reset
    drop(pool.acquire().await?);
    let rows = history(&observer, TABLE).await?;
    let locks = advisory_locks(&observer).await?;
    pool.close(Some(Duration::from_secs(5))).await;
    observer.close().await;
    Ok((rows, locks))
}

#[tokio::test]
async fn cancelling_a_transactional_migration_rolls_back_and_frees_the_lock() -> TestResult {
    let Some(db) = TestDb::new().await? else {
        return Ok(());
    };
    let (rows, locks) =
        cancelled_run(&db, "CREATE TABLE cancelled (i int); SELECT pg_sleep(600)").await?;
    assert!(rows.is_empty(), "{rows:?}");
    assert_eq!(locks, 0);
    let mut conn = db.connect().await?;
    assert_eq!(
        count(
            &conn,
            "SELECT count(*) FROM pg_tables WHERE tablename = 'cancelled'"
        )
        .await?,
        0
    );
    conn.close().await;
    db.finish().await
}

#[tokio::test]
async fn cancelling_a_no_transaction_migration_leaves_a_dirty_row() -> TestResult {
    let Some(db) = TestDb::new().await? else {
        return Ok(());
    };
    let (rows, locks) = cancelled_run(&db, "-- no-transaction\nSELECT pg_sleep(600)").await?;
    assert_eq!(rows, [(1, false)]);
    assert_eq!(locks, 0);
    db.finish().await
}

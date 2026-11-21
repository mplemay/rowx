// pub, so the shared helpers are reachable from the crate root and need no visibility waiver
pub mod common;

use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::time::Duration;

use common::{LONG_QUERY, TestResult, connect, file, ints, pragma, within};
use rowx_core::migrate::{
    Migration, MigrationKind, MigrationStatus, PlanError, PlanOptions, RevertTarget, SchemaName,
    TableName, checksum,
};
use rowx_sqlite::conn::Connection;
use rowx_sqlite::error::Error;
use rowx_sqlite::migrate::{
    ConfigError, MigrateConfig, MigrateError, MigrationDetail, info, list_applied, revert,
    revert_dry, run, run_dry, skip, skip_dry,
};
use tempfile::TempDir;

const TABLE: &str = "_rowx_migrations";
// a flag nobody sets, for runs that are never cancelled
static LIVE: AtomicBool = AtomicBool::new(false);

fn versions(details: Vec<MigrationDetail>) -> Vec<i64> {
    details.into_iter().map(|d| d.info.version).collect()
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

fn table(name: &str) -> TableName {
    TableName::parse(name).unwrap_or_else(|e| unreachable!("{e}"))
}

fn config() -> MigrateConfig {
    MigrateConfig::new(table(TABLE), &[]).unwrap_or_else(|e| unreachable!("{e}"))
}

fn target(version: i64) -> PlanOptions {
    PlanOptions {
        target: Some(version),
        ..PlanOptions::default()
    }
}

async fn count(conn: &Connection, sql: &str) -> TestResult<i64> {
    ints(conn, sql)
        .await?
        .first()
        .copied()
        .ok_or_else(|| "no row".into())
}

async fn table_exists(conn: &Connection, name: &str) -> TestResult<bool> {
    Ok(count(
        conn,
        &format!("SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = '{name}'"),
    )
    .await?
        == 1)
}

// (version, success) of every history row
async fn history(conn: &Connection) -> TestResult<Vec<(i64, bool)>> {
    let versions = ints(
        conn,
        &format!("SELECT version FROM {TABLE} ORDER BY version"),
    )
    .await?;
    let success = ints(
        conn,
        &format!("SELECT CAST(success AS INTEGER) FROM {TABLE} ORDER BY version"),
    )
    .await?;
    Ok(versions
        .into_iter()
        .zip(success)
        .map(|(version, success)| (version, success == 1))
        .collect())
}

const fn failed_with(err: &MigrateError) -> Option<(&str, i64)> {
    match err {
        MigrateError::Failed {
            version,
            source: Error::Sqlite { name, .. },
            ..
        } => Some((name, *version)),
        _ => None,
    }
}

#[tokio::test]
async fn applies_reapplies_and_stops_at_a_target() -> TestResult {
    let dir = TempDir::new()?;
    let conn = connect(&file(&dir)).await?;
    let all = [
        simple(1, "CREATE TABLE a (i int)"),
        simple(2, "CREATE TABLE b (i int)"),
        simple(3, "CREATE TABLE c (i int)"),
    ];

    let first = versions(run(&conn, &config(), &all, target(2), &LIVE).await?);
    assert_eq!(first, [1, 2]);
    assert_eq!(history(&conn).await?, [(1, true), (2, true)]);

    let rest = versions(run(&conn, &config(), &all, PlanOptions::default(), &LIVE).await?);
    assert_eq!(rest, [3]);
    let again = run(&conn, &config(), &all, PlanOptions::default(), &LIVE).await?;
    assert_eq!(versions(again), Vec::<i64>::new());
    assert_eq!(history(&conn).await?.len(), 3);

    // the description and the up checksum are recorded, and execution_time replaces the -1
    let applied = list_applied(&conn, &config()).await?;
    assert_eq!(applied[1].migration.checksum, all[1].checksum.to_vec());
    assert!(applied[1].execution_time >= 0);
    assert!(applied[1].installed_on_micros > 0);
    assert_eq!(
        count(
            &conn,
            &format!("SELECT count(*) FROM {TABLE} WHERE description = 'm2'")
        )
        .await?,
        1
    );
    // the connection is out of every transaction
    assert!(conn.is_autocommit().await?);
    Ok(())
}

#[tokio::test]
async fn a_failing_transactional_migration_rolls_back_and_records_nothing() -> TestResult {
    let dir = TempDir::new()?;
    let conn = connect(&file(&dir)).await?;
    let all = [
        simple(1, "CREATE TABLE ok (i int)"),
        simple(
            2,
            "CREATE TABLE partial (i int); INSERT INTO missing VALUES (1)",
        ),
    ];

    let err = run(&conn, &config(), &all, PlanOptions::default(), &LIVE)
        .await
        .err()
        .ok_or("expected a failure")?;
    assert_eq!(failed_with(&err), Some(("SQLITE_ERROR", 2)), "{err:?}");
    // the results of the migrations applied before the failure travel with it, and the row isn't dirty
    match &err {
        MigrateError::Failed { dirty, applied, .. } => {
            assert!(!dirty);
            assert_eq!(
                applied.iter().map(|d| d.info.version).collect::<Vec<_>>(),
                [1]
            );
        }
        other => return Err(format!("unexpected {other:?}").into()),
    }
    assert!(err.to_string().starts_with("migration 2 failed: "), "{err}");

    assert_eq!(history(&conn).await?, [(1, true)]);
    assert!(!table_exists(&conn, "partial").await?);
    assert!(conn.is_autocommit().await?);
    Ok(())
}

#[tokio::test]
async fn a_script_with_its_own_transaction_statements_is_rejected_before_it_runs() -> TestResult {
    let dir = TempDir::new()?;
    let conn = connect(&file(&dir)).await?;
    for (index, sql) in [
        "CREATE TABLE early (i int); BEGIN; CREATE TABLE late (i int); COMMIT;",
        "CREATE TABLE early (i int); COMMIT;",
        "CREATE TABLE early (i int); ROLLBACK;",
    ]
    .into_iter()
    .enumerate()
    {
        let err = run(
            &conn,
            &config(),
            &[simple(1, sql)],
            PlanOptions::default(),
            &LIVE,
        )
        .await
        .err()
        .ok_or("expected a rejection")?;
        assert_eq!(
            failed_with(&err),
            Some(("SQLITE_AUTH", 1)),
            "{index}: {err:?}"
        );
        assert!(
            err.to_string()
                .contains("must not contain BEGIN, COMMIT or ROLLBACK"),
            "{err}"
        );
        // the whole file rolled back, and nothing is recorded
        assert!(!table_exists(&conn, "early").await?, "{index}");
        assert_eq!(history(&conn).await?, Vec::new(), "{index}");
        assert!(conn.is_autocommit().await?, "{index}");
    }
    Ok(())
}

#[tokio::test]
async fn a_no_transaction_script_with_its_own_begin_is_rejected_and_stays_dirty() -> TestResult {
    let dir = TempDir::new()?;
    let conn = connect(&file(&dir)).await?;
    let all = [simple(
        1,
        "-- no-transaction\nBEGIN; CREATE TABLE inside (i int); COMMIT;",
    )];
    let err = run(&conn, &config(), &all, PlanOptions::default(), &LIVE)
        .await
        .err()
        .ok_or("expected a rejection")?;
    assert_eq!(failed_with(&err), Some(("SQLITE_AUTH", 1)), "{err:?}");
    assert!(matches!(err, MigrateError::Failed { dirty: true, .. }));
    assert!(!table_exists(&conn, "inside").await?);
    assert_eq!(history(&conn).await?, [(1, false)]);
    assert!(conn.is_autocommit().await?);
    Ok(())
}

#[tokio::test]
async fn a_no_transaction_file_can_vacuum() -> TestResult {
    let dir = TempDir::new()?;
    let conn = connect(&file(&dir)).await?;
    let all = [
        simple(
            1,
            "CREATE TABLE t (i int); INSERT INTO t VALUES (1), (2), (3);",
        ),
        simple(2, "-- no-transaction\nVACUUM;"),
    ];
    run(&conn, &config(), &all, PlanOptions::default(), &LIVE).await?;
    assert_eq!(history(&conn).await?, [(1, true), (2, true)]);
    assert_eq!(ints(&conn, "SELECT count(*) FROM t").await?, [3]);
    assert!(conn.is_autocommit().await?);
    Ok(())
}

#[tokio::test]
async fn a_no_transaction_file_can_change_the_journal_mode() -> TestResult {
    let dir = TempDir::new()?;
    let conn = connect(&file(&dir)).await?;
    let all = [simple(
        1,
        "-- no-transaction\nPRAGMA journal_mode = DELETE;\nPRAGMA journal_mode = WAL;",
    )];
    run(&conn, &config(), &all, PlanOptions::default(), &LIVE).await?;
    assert_eq!(history(&conn).await?, [(1, true)]);
    assert!(conn.is_autocommit().await?);
    Ok(())
}

#[tokio::test]
async fn triggers_and_savepoints_are_not_transaction_statements() -> TestResult {
    let dir = TempDir::new()?;
    let conn = connect(&file(&dir)).await?;
    let all = [simple(
        1,
        "CREATE TABLE t (i int); CREATE TABLE log (i int);\n\
         CREATE TRIGGER t_log AFTER INSERT ON t BEGIN INSERT INTO log VALUES (new.i); END;\n\
         SAVEPOINT s; INSERT INTO t VALUES (7); RELEASE s;",
    )];
    run(&conn, &config(), &all, PlanOptions::default(), &LIVE).await?;
    assert_eq!(ints(&conn, "SELECT i FROM log").await?, [7]);
    // the driver's refusal was only for the length of the script
    conn.simple("BEGIN").await?;
    conn.simple("COMMIT").await?;
    Ok(())
}

#[tokio::test]
async fn a_failing_no_transaction_migration_is_dirty_and_blocks_the_next_run() -> TestResult {
    let dir = TempDir::new()?;
    let conn = connect(&file(&dir)).await?;
    let all = [
        simple(
            1,
            "-- no-transaction\nCREATE TABLE t (i int); INSERT INTO missing VALUES (1)",
        ),
        simple(2, "CREATE TABLE after (i int)"),
    ];

    let err = run(&conn, &config(), &all, PlanOptions::default(), &LIVE)
        .await
        .err()
        .ok_or("expected a failure")?;
    assert_eq!(failed_with(&err), Some(("SQLITE_ERROR", 1)), "{err:?}");
    assert!(
        matches!(err, MigrateError::Failed { dirty: true, ref applied, .. } if applied.is_empty()),
        "{err:?}"
    );
    assert_eq!(history(&conn).await?, [(1, false)]);
    // outside any transaction, what ran before the failure stays
    assert!(table_exists(&conn, "t").await?);

    let next = run(&conn, &config(), &all, PlanOptions::default(), &LIVE)
        .await
        .err()
        .ok_or("expected the dirty row to block")?;
    assert!(
        matches!(next, MigrateError::Plan(PlanError::Dirty { version: 1 })),
        "{next:?}"
    );
    assert!(!table_exists(&conn, "after").await?);

    let details = info(&conn, &config(), &all).await?;
    assert_eq!(details[0].info.status, MigrationStatus::Dirty);
    Ok(())
}

// An interrupt only reaches a statement that is running, so one sent between two statements is lost. It is
// repeated until the task ends, which makes the outcome independent of when the statement starts.
async fn interrupt_until_finished(
    conn: &Connection,
    task: tokio::task::JoinHandle<Result<(), MigrateError>>,
) -> TestResult<Result<(), MigrateError>> {
    let mut task = task;
    within(async {
        loop {
            conn.interrupt_handle().interrupt();
            tokio::select! {
                done = &mut task => return Ok(done?),
                () = tokio::time::sleep(Duration::from_millis(1)) => {}
            }
        }
    })
    .await?
}

// set by a progress handler that only a long statement reaches: while it is set, the file is running
static LONG_RUNNING: AtomicBool = AtomicBool::new(false);

fn long_running() -> bool {
    LONG_RUNNING.store(true, Ordering::Release);
    false
}

// the version is claimed by a committed row before the file runs, so a crash in the file leaves it
#[tokio::test]
async fn a_no_transaction_file_claims_its_version_before_it_runs() -> TestResult {
    let dir = TempDir::new()?;
    let migrator = Arc::new(connect(&file(&dir)).await?);
    let observer = connect(&file(&dir)).await?;
    let all = vec![simple(
        1,
        &format!("-- no-transaction\nCREATE TABLE gate (i int);\n{LONG_QUERY};"),
    )];
    let task = {
        let migrator = Arc::clone(&migrator);
        tokio::spawn(async move {
            run(&migrator, &config(), &all, PlanOptions::default(), &LIVE)
                .await
                .map(|_| ())
        })
    };
    // `gate` is created by the file itself, so once it is visible the file is running
    within(async {
        loop {
            if table_exists(&observer, "gate").await? {
                return Ok::<_, Box<dyn std::error::Error>>(());
            }
            tokio::task::yield_now().await;
        }
    })
    .await??;
    // seen from another connection, the claim is committed and dirty
    assert_eq!(history(&observer).await?, [(1, false)]);
    // the file does not finish: stop it the way a crash would
    let err = interrupt_until_finished(&migrator, task)
        .await?
        .err()
        .ok_or("the interrupted file should fail")?;
    assert!(
        matches!(err, MigrateError::Failed { dirty: true, .. }),
        "{err:?}"
    );
    assert_eq!(history(&observer).await?, [(1, false)]);
    Ok(())
}

// a failure of the last history write leaves the claim row as it was, so the version stays dirty
#[tokio::test]
async fn a_failing_final_history_write_is_a_dirty_failure() -> TestResult {
    let dir = TempDir::new()?;
    let conn = connect(&file(&dir)).await?;
    // the file drops the history table, so the `UPDATE` that follows it cannot succeed
    let all = [simple(
        1,
        &format!("-- no-transaction\nCREATE TABLE t (i int);\nDROP TABLE {TABLE};"),
    )];
    let err = run(&conn, &config(), &all, PlanOptions::default(), &LIVE)
        .await
        .err()
        .ok_or("expected a failure")?;
    assert!(
        matches!(
            err,
            MigrateError::Failed {
                version: 1,
                dirty: true,
                ..
            }
        ),
        "{err:?}"
    );
    assert!(table_exists(&conn, "t").await?);

    let dir = TempDir::new()?;
    let conn = connect(&file(&dir)).await?;
    let [up, down] = reversible(
        1,
        "CREATE TABLE a (i int)",
        &format!("-- no-transaction\nDROP TABLE a;\nDROP TABLE {TABLE};"),
    );
    let all = [up, down];
    run(&conn, &config(), &all, PlanOptions::default(), &LIVE).await?;
    let err = revert(&conn, &config(), &all, RevertTarget::Latest, false, &LIVE)
        .await
        .err()
        .ok_or("expected a failure")?;
    assert!(
        matches!(
            err,
            MigrateError::Failed {
                version: 1,
                dirty: true,
                ..
            }
        ),
        "{err:?}"
    );
    assert!(!table_exists(&conn, "a").await?);
    Ok(())
}

// an unreleased SAVEPOINT starts a transaction in autocommit mode; it is rolled back and the file is dirty
#[tokio::test]
async fn a_no_transaction_file_that_leaves_a_transaction_open_is_rolled_back_and_dirty()
-> TestResult {
    let dir = TempDir::new()?;
    let conn = connect(&file(&dir)).await?;
    let all = [simple(
        1,
        "-- no-transaction\nPRAGMA foreign_keys = OFF;\nCREATE TABLE before_savepoint (i INTEGER);\nSAVEPOINT s;\nCREATE TABLE t (i INTEGER);",
    )];
    let err = run(&conn, &config(), &all, PlanOptions::default(), &LIVE)
        .await
        .err()
        .ok_or("expected a failure")?;
    assert!(
        matches!(&err, MigrateError::Failed { version: 1, dirty: true, source: Error::Sqlite { message, .. }, .. }
            if message.contains("must not leave a transaction open")),
        "{err:?}"
    );
    assert!(conn.is_autocommit().await?);
    // autocommit already committed the work before the SAVEPOINT; only the work since it is undone
    assert!(table_exists(&conn, "before_savepoint").await?);
    assert!(!table_exists(&conn, "t").await?);
    assert_eq!(pragma(&conn, "foreign_keys").await?, 1);
    assert_eq!(history(&conn).await?, [(1, false)]);
    Ok(())
}

#[tokio::test]
async fn a_no_transaction_revert_that_leaves_a_transaction_open_is_rolled_back_and_dirty()
-> TestResult {
    let dir = TempDir::new()?;
    let conn = connect(&file(&dir)).await?;
    let [up, down] = reversible(
        1,
        "CREATE TABLE t (i int)",
        "-- no-transaction\nPRAGMA foreign_keys = OFF;\nSAVEPOINT s;\nDROP TABLE t;",
    );
    let all = [up, down];
    run(&conn, &config(), &all, PlanOptions::default(), &LIVE).await?;
    let err = revert(&conn, &config(), &all, RevertTarget::Latest, false, &LIVE)
        .await
        .err()
        .ok_or("expected a failure")?;
    assert!(
        matches!(&err, MigrateError::Failed { version: 1, dirty: true, source: Error::Sqlite { message, .. }, .. }
            if message.contains("must not leave a transaction open")),
        "{err:?}"
    );
    assert!(conn.is_autocommit().await?);
    assert!(table_exists(&conn, "t").await?);
    assert_eq!(pragma(&conn, "foreign_keys").await?, 1);
    assert_eq!(history(&conn).await?, [(1, false)]);
    Ok(())
}

// a transactional file that is interrupted mid-script leaves nothing behind and a usable connection
#[tokio::test]
async fn interrupting_a_transactional_migration_mid_script_rolls_it_back() -> TestResult {
    let dir = TempDir::new()?;
    let migrator = Arc::new(connect(&file(&dir)).await?);
    let observer = connect(&file(&dir)).await?;
    // a handler that only a long statement reaches tells the test the file is running
    migrator
        .run_raw(|raw| raw.progress_handler(100_000, Some(long_running)))
        .await??;
    let all = vec![simple(
        1,
        &format!("CREATE TABLE gate (i int);\n{LONG_QUERY};"),
    )];
    let task = {
        let migrator = Arc::clone(&migrator);
        tokio::spawn(async move {
            run(&migrator, &config(), &all, PlanOptions::default(), &LIVE)
                .await
                .map(|_| ())
        })
    };
    within(async {
        while !LONG_RUNNING.load(Ordering::Acquire) {
            tokio::task::yield_now().await;
        }
    })
    .await?;
    let err = interrupt_until_finished(&migrator, task)
        .await?
        .err()
        .ok_or("the interrupted file should fail")?;
    assert!(
        matches!(
            err,
            MigrateError::Failed {
                version: 1,
                dirty: false,
                ..
            }
        ),
        "{err:?}"
    );
    assert!(migrator.is_autocommit().await?);
    assert_eq!(
        count(&observer, &format!("SELECT count(*) FROM {TABLE}")).await?,
        0
    );
    assert!(!table_exists(&observer, "gate").await?);
    assert!(!table_exists(&migrator, "gate").await?);
    // the authorizer that refuses transaction statements is gone
    migrator.simple("BEGIN").await?;
    migrator.simple("ROLLBACK").await?;
    Ok(())
}

#[tokio::test]
async fn a_table_rebuild_with_foreign_keys_off_runs_in_a_no_transaction_file() -> TestResult {
    let dir = TempDir::new()?;
    let conn = connect(&file(&dir)).await?;
    let all = [
        simple(
            1,
            "CREATE TABLE parent (id INTEGER PRIMARY KEY, name TEXT);\n\
             CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER NOT NULL \
             REFERENCES parent (id) ON DELETE CASCADE);\n\
             INSERT INTO parent VALUES (1, 'a'), (2, 'b');\n\
             INSERT INTO child VALUES (10, 1), (20, 2);",
        ),
        simple(
            2,
            "-- no-transaction\n\
             PRAGMA foreign_keys = OFF;\n\
             CREATE TABLE parent_new (id INTEGER PRIMARY KEY, name TEXT, extra TEXT);\n\
             INSERT INTO parent_new (id, name) SELECT id, name FROM parent;\n\
             DROP TABLE parent;\n\
             ALTER TABLE parent_new RENAME TO parent;\n\
             PRAGMA foreign_keys = ON;",
        ),
    ];
    assert_eq!(pragma(&conn, "foreign_keys").await?, 1);
    run(&conn, &config(), &all, PlanOptions::default(), &LIVE).await?;
    // with the keys on, dropping the parent would have cascaded into the child
    assert_eq!(ints(&conn, "SELECT count(*) FROM child").await?, [2]);
    assert_eq!(ints(&conn, "SELECT count(*) FROM parent").await?, [2]);
    assert_eq!(
        ints(&conn, "SELECT count(*) FROM pragma_table_info('parent')").await?,
        [3]
    );
    assert_eq!(history(&conn).await?, [(1, true), (2, true)]);
    Ok(())
}

#[tokio::test]
async fn a_failed_no_transaction_file_leaves_foreign_keys_on() -> TestResult {
    let dir = TempDir::new()?;
    let conn = connect(&file(&dir)).await?;
    let all = [simple(
        1,
        "-- no-transaction\nPRAGMA foreign_keys = OFF;\nINSERT INTO missing VALUES (1);",
    )];
    run(&conn, &config(), &all, PlanOptions::default(), &LIVE)
        .await
        .err()
        .ok_or("expected a failure")?;
    assert_eq!(pragma(&conn, "foreign_keys").await?, 1);
    Ok(())
}

#[tokio::test]
async fn revert_reverts_newest_first_to_a_target_and_to_zero() -> TestResult {
    let dir = TempDir::new()?;
    let conn = connect(&file(&dir)).await?;
    let mut all = Vec::new();
    for (version, name) in [(1, "a"), (2, "b"), (3, "c")] {
        all.extend(reversible(
            version,
            &format!("CREATE TABLE {name} (i int)"),
            &format!("DROP TABLE {name}"),
        ));
    }
    run(&conn, &config(), &all, PlanOptions::default(), &LIVE).await?;

    // without a target only the latest is reverted, and its time is how long the revert took
    let latest = revert(&conn, &config(), &all, RevertTarget::Latest, false, &LIVE).await?;
    assert_eq!(versions(latest.clone()), [3]);
    assert!(latest[0].execution_time.is_some_and(|nanos| nanos >= 0));
    assert!(!table_exists(&conn, "c").await?);

    run(&conn, &config(), &all, PlanOptions::default(), &LIVE).await?;
    let above = revert(&conn, &config(), &all, RevertTarget::Above(1), false, &LIVE).await?;
    assert_eq!(versions(above), [3, 2]);
    assert_eq!(history(&conn).await?, [(1, true)]);

    let rest = revert(&conn, &config(), &all, RevertTarget::All, false, &LIVE).await?;
    assert_eq!(versions(rest), [1]);
    assert_eq!(history(&conn).await?, Vec::new());
    assert!(!table_exists(&conn, "a").await?);
    Ok(())
}

#[tokio::test]
async fn revert_without_a_down_file_reverts_nothing() -> TestResult {
    let dir = TempDir::new()?;
    let conn = connect(&file(&dir)).await?;
    let [up, down] = reversible(1, "CREATE TABLE a (i int)", "DROP TABLE a");
    let all = [up, down, simple(2, "CREATE TABLE b (i int)")];
    run(&conn, &config(), &all, PlanOptions::default(), &LIVE).await?;
    let err = revert(&conn, &config(), &all, RevertTarget::All, false, &LIVE)
        .await
        .err()
        .ok_or("version 2 has no down file")?;
    assert!(
        matches!(
            err,
            MigrateError::Plan(PlanError::MissingDown { version: 2 })
        ),
        "{err:?}"
    );
    assert_eq!(history(&conn).await?, [(1, true), (2, true)]);
    assert!(table_exists(&conn, "a").await?);
    Ok(())
}

#[tokio::test]
async fn a_failing_transactional_revert_keeps_the_row_and_the_schema() -> TestResult {
    let dir = TempDir::new()?;
    let conn = connect(&file(&dir)).await?;
    let [up, down] = reversible(
        1,
        "CREATE TABLE a (i int)",
        "DROP TABLE a; DROP TABLE missing",
    );
    let all = [up, down];
    run(&conn, &config(), &all, PlanOptions::default(), &LIVE).await?;
    let err = revert(&conn, &config(), &all, RevertTarget::Latest, false, &LIVE)
        .await
        .err()
        .ok_or("expected a failure")?;
    assert_eq!(failed_with(&err), Some(("SQLITE_ERROR", 1)), "{err:?}");
    assert_eq!(history(&conn).await?, [(1, true)]);
    assert!(table_exists(&conn, "a").await?);
    Ok(())
}

#[tokio::test]
async fn a_no_transaction_revert_deletes_the_row_and_a_failing_one_leaves_it_dirty() -> TestResult {
    let dir = TempDir::new()?;
    let conn = connect(&file(&dir)).await?;
    let [up1, down1] = reversible(
        1,
        "CREATE TABLE a (i int)",
        "-- no-transaction\nDROP TABLE a",
    );
    let [up2, down2] = reversible(
        2,
        "CREATE TABLE b (i int)",
        "-- no-transaction\nDROP TABLE b; DROP TABLE missing",
    );
    let all = [up1, down1, up2, down2];
    run(&conn, &config(), &all, PlanOptions::default(), &LIVE).await?;

    let err = revert(&conn, &config(), &all, RevertTarget::Latest, false, &LIVE)
        .await
        .err()
        .ok_or("expected a failure")?;
    assert!(
        matches!(
            err,
            MigrateError::Failed {
                version: 2,
                dirty: true,
                ..
            }
        ),
        "{err:?}"
    );
    assert_eq!(history(&conn).await?, [(1, true), (2, false)]);

    // the dirty row blocks everything else
    let blocked = revert(&conn, &config(), &all, RevertTarget::All, false, &LIVE)
        .await
        .err()
        .ok_or("expected the dirty row to block")?;
    assert!(
        matches!(blocked, MigrateError::Plan(PlanError::Dirty { version: 2 })),
        "{blocked:?}"
    );

    conn.simple(&format!("DELETE FROM {TABLE} WHERE version = 2"))
        .await?;
    let done = revert(&conn, &config(), &all, RevertTarget::Latest, false, &LIVE).await?;
    assert_eq!(versions(done), [1]);
    assert_eq!(history(&conn).await?, Vec::new());
    assert!(!table_exists(&conn, "a").await?);
    Ok(())
}

#[tokio::test]
async fn skip_records_without_running() -> TestResult {
    let dir = TempDir::new()?;
    let conn = connect(&file(&dir)).await?;
    let all = [
        simple(1, "CREATE TABLE a (i int)"),
        simple(2, "CREATE TABLE b (i int)"),
        simple(3, "CREATE TABLE c (i int)"),
    ];
    let done = skip(&conn, &config(), &all, target(2), &LIVE).await?;
    assert_eq!(versions(done), [1, 2]);
    assert!(!table_exists(&conn, "a").await?);
    assert_eq!(history(&conn).await?, [(1, true), (2, true)]);
    // a skipped row has no execution time, and the checksum is recorded
    let applied = list_applied(&conn, &config()).await?;
    assert_eq!(applied[0].execution_time, -1);
    assert_eq!(applied[0].migration.checksum, all[0].checksum.to_vec());

    // the rest still runs
    let rest = run(&conn, &config(), &all, PlanOptions::default(), &LIVE).await?;
    assert_eq!(versions(rest), [3]);
    assert!(table_exists(&conn, "c").await?);
    assert_eq!(
        versions(skip(&conn, &config(), &all, PlanOptions::default(), &LIVE).await?),
        Vec::<i64>::new()
    );
    Ok(())
}

#[tokio::test]
async fn a_dry_run_plans_like_the_real_one_and_changes_nothing() -> TestResult {
    let dir = TempDir::new()?;
    let conn = connect(&file(&dir)).await?;
    let mut all = Vec::new();
    for (version, name) in [(1, "a"), (2, "b")] {
        all.extend(reversible(
            version,
            &format!("CREATE TABLE {name} (i int)"),
            &format!("DROP TABLE {name}"),
        ));
    }

    // on a fresh database nothing is created, not even the history table
    let planned = run_dry(&conn, &config(), &all, PlanOptions::default()).await?;
    assert_eq!(versions(planned.clone()), [1, 2]);
    assert!(planned.iter().all(|d| d.execution_time.is_none()));
    assert_eq!(
        versions(skip_dry(&conn, &config(), &all, PlanOptions::default()).await?),
        [1, 2]
    );
    assert_eq!(
        versions(revert_dry(&conn, &config(), &all, RevertTarget::All, false).await?),
        Vec::<i64>::new()
    );
    assert!(!table_exists(&conn, TABLE).await?);
    assert!(!table_exists(&conn, "a").await?);

    run(&conn, &config(), &all, PlanOptions::default(), &LIVE).await?;
    let reverting = revert_dry(&conn, &config(), &all, RevertTarget::Latest, false).await?;
    assert_eq!(versions(reverting.clone()), [2]);
    assert_eq!(reverting[0].info.status, MigrationStatus::Pending);
    assert_eq!(history(&conn).await?, [(1, true), (2, true)]);
    assert!(table_exists(&conn, "b").await?);

    // a dry run reports the errors a real one does
    let changed = [
        simple(1, "CREATE TABLE a (i bigint)"),
        simple(2, "CREATE TABLE b (i int)"),
    ];
    let err = run_dry(&conn, &config(), &changed, PlanOptions::default())
        .await
        .err()
        .ok_or("expected a checksum error")?;
    assert!(
        matches!(
            err,
            MigrateError::Plan(PlanError::Checksum { version: 1, .. })
        ),
        "{err:?}"
    );
    Ok(())
}

#[tokio::test]
async fn a_modified_missing_or_out_of_order_migration_is_a_plan_error() -> TestResult {
    let dir = TempDir::new()?;
    let conn = connect(&file(&dir)).await?;
    let all = [
        simple(1, "CREATE TABLE a (i int)"),
        simple(2, "CREATE TABLE b (i int)"),
        simple(3, "CREATE TABLE c (i int)"),
    ];
    run(&conn, &config(), &all[1..], PlanOptions::default(), &LIVE).await?;

    // version 1 is older than the latest applied version 3
    let order = run(&conn, &config(), &all, target(1), &LIVE).await;
    assert!(
        matches!(
            order,
            Err(MigrateError::Plan(PlanError::Order {
                pending: 1,
                latest: 3
            }))
        ),
        "{order:?}"
    );
    let allowed = PlanOptions {
        allow_out_of_order: true,
        ..target(1)
    };
    assert_eq!(
        versions(run(&conn, &config(), &all, allowed, &LIVE).await?),
        [1]
    );

    let modified = [
        simple(1, "CREATE TABLE a (i int)"),
        simple(2, "CREATE TABLE b (i bigint)"),
        simple(3, "CREATE TABLE c (i int)"),
    ];
    let err = run(&conn, &config(), &modified, PlanOptions::default(), &LIVE).await;
    assert!(
        matches!(
            err,
            Err(MigrateError::Plan(PlanError::Checksum { version: 2, .. }))
        ),
        "{err:?}"
    );

    let err = run(&conn, &config(), &all[..2], PlanOptions::default(), &LIVE).await;
    assert!(
        matches!(&err, Err(MigrateError::Plan(PlanError::Missing { versions })) if versions == &[3]),
        "{err:?}"
    );
    let ignoring = PlanOptions {
        ignore_missing: true,
        ..PlanOptions::default()
    };
    assert_eq!(
        versions(run(&conn, &config(), &all[..2], ignoring, &LIVE).await?),
        Vec::<i64>::new()
    );
    Ok(())
}

#[tokio::test]
async fn info_creates_nothing_and_reports_every_state() -> TestResult {
    let dir = TempDir::new()?;
    let conn = connect(&file(&dir)).await?;
    let all = [
        simple(1, "CREATE TABLE a (i int)"),
        simple(2, "CREATE TABLE b (i int)"),
        simple(3, "CREATE TABLE c (i int)"),
    ];
    let fresh = info(&conn, &config(), &all).await?;
    assert!(
        fresh
            .iter()
            .all(|d| d.info.status == MigrationStatus::Pending)
    );
    assert!(!table_exists(&conn, TABLE).await?);

    run(&conn, &config(), &all[..2], PlanOptions::default(), &LIVE).await?;
    conn.simple(&format!(
        "INSERT INTO {TABLE} (version, description, success, checksum, execution_time) \
         VALUES (9, 'gone', 1, x'00', 0), (4, 'half', 0, x'00', -1)"
    ))
    .await?;
    let local = [
        simple(1, "CREATE TABLE a (i int)"),
        simple(2, "CREATE TABLE b (i bigint)"),
        simple(3, "CREATE TABLE c (i int)"),
        simple(4, "SELECT 1"),
    ];
    let statuses: Vec<_> = info(&conn, &config(), &local)
        .await?
        .into_iter()
        .map(|d| (d.info.version, d.info.status))
        .collect();
    assert_eq!(
        statuses,
        [
            (1, MigrationStatus::Applied),
            (2, MigrationStatus::Modified),
            (3, MigrationStatus::Pending),
            (4, MigrationStatus::Dirty),
            (9, MigrationStatus::Missing),
        ]
    );
    Ok(())
}

#[tokio::test]
async fn a_set_cancel_flag_stops_before_any_step() -> TestResult {
    let dir = TempDir::new()?;
    let conn = connect(&file(&dir)).await?;
    let all = [simple(1, "CREATE TABLE a (i int)")];
    let cancelled = AtomicBool::new(true);
    let err = run(&conn, &config(), &all, PlanOptions::default(), &cancelled)
        .await
        .err()
        .ok_or("expected a cancellation")?;
    assert!(matches!(err, MigrateError::Cancelled), "{err:?}");
    assert_eq!(history(&conn).await?, Vec::new());
    assert!(!table_exists(&conn, "a").await?);
    Ok(())
}

#[tokio::test]
async fn the_history_table_is_a_plain_name_or_main_and_there_are_no_schemas_to_create() -> TestResult
{
    let dir = TempDir::new()?;
    let conn = connect(&file(&dir)).await?;

    let schema = MigrateConfig::new(table("other.history"), &[]);
    assert!(
        matches!(&schema, Err(ConfigError::Schema { schema, .. }) if schema == "other"),
        "{schema:?}"
    );
    assert!(
        schema
            .err()
            .is_some_and(|e| e.to_string().contains("no schemas"))
    );
    let create = MigrateConfig::new(table("history"), &[SchemaName::parse("extra")?]);
    assert_eq!(create, Err(ConfigError::CreateSchemas));

    let main = MigrateConfig::new(table("main.history"), &[])?;
    let all = [simple(1, "CREATE TABLE a (i int)")];
    run(&conn, &main, &all, PlanOptions::default(), &LIVE).await?;
    assert!(table_exists(&conn, "history").await?);
    assert_eq!(
        info(&conn, &main, &all).await?[0].info.status,
        MigrationStatus::Applied
    );
    Ok(())
}

// a migrator that waits for the write lock counts how often SQLite asked it to; the counter is how a test
// knows the migrator is blocked, so it never has to guess with a delay
static FIRST_WAITS: AtomicUsize = AtomicUsize::new(0);
static SECOND_WAITS: AtomicUsize = AtomicUsize::new(0);

fn first_waits(_: i32) -> bool {
    FIRST_WAITS.fetch_add(1, Ordering::AcqRel);
    std::thread::sleep(Duration::from_millis(1));
    true
}

fn second_waits(_: i32) -> bool {
    SECOND_WAITS.fetch_add(1, Ordering::AcqRel);
    std::thread::sleep(Duration::from_millis(1));
    true
}

async fn wait_for(counter: &'static AtomicUsize) -> TestResult {
    within(async {
        while counter.load(Ordering::Acquire) == 0 {
            tokio::task::yield_now().await;
        }
    })
    .await
}

#[tokio::test]
async fn two_migrators_on_one_database_wait_for_the_lock_and_apply_each_version_once() -> TestResult
{
    let dir = TempDir::new()?;
    let holder = connect(&file(&dir)).await?;
    let first = Arc::new(connect(&file(&dir)).await?);
    let second = Arc::new(connect(&file(&dir)).await?);
    first
        .run_raw(|raw| raw.busy_handler(Some(first_waits)))
        .await??;
    second
        .run_raw(|raw| raw.busy_handler(Some(second_waits)))
        .await??;
    let all = Arc::new(vec![
        simple(1, "CREATE TABLE a (i int)"),
        simple(2, "CREATE TABLE b (i int)"),
        simple(3, "CREATE TABLE c (i int)"),
    ]);

    // both start while a third connection holds the write lock
    holder
        .run_raw(|raw| raw.execute_batch("BEGIN IMMEDIATE"))
        .await??;
    let start = |conn: Arc<Connection>| {
        let all = Arc::clone(&all);
        tokio::spawn(
            async move { run(&conn, &config(), &all, PlanOptions::default(), &LIVE).await },
        )
    };
    let (one, two) = (start(Arc::clone(&first)), start(Arc::clone(&second)));
    wait_for(&FIRST_WAITS).await?;
    wait_for(&SECOND_WAITS).await?;
    assert!(!one.is_finished() && !two.is_finished());
    holder
        .run_raw(|raw| raw.execute_batch("ROLLBACK"))
        .await??;

    let mut applied = versions(within(one).await???);
    applied.extend(versions(within(two).await???));
    applied.sort_unstable();
    // together they applied every version, and none of them twice
    assert_eq!(applied, [1, 2, 3]);
    assert_eq!(history(&holder).await?, [(1, true), (2, true), (3, true)]);
    Ok(())
}

#[tokio::test]
async fn a_migrator_gives_up_after_the_busy_timeout_with_a_clear_error() -> TestResult {
    let dir = TempDir::new()?;
    let holder = connect(&file(&dir)).await?;
    let mut options = file(&dir);
    options.busy_timeout = Duration::from_millis(50);
    let conn = connect(&options).await?;
    let all = [
        simple(1, "CREATE TABLE a (i int)"),
        simple(2, "CREATE TABLE b (i int)"),
    ];

    // the history table does not exist yet, so creating it needs the lock
    holder
        .run_raw(|raw| raw.execute_batch("BEGIN IMMEDIATE"))
        .await??;
    let err = run(&conn, &config(), &all, PlanOptions::default(), &LIVE)
        .await
        .err()
        .ok_or("the lock is held")?;
    assert!(matches!(err, MigrateError::Busy(_)), "{err:?}");
    assert!(
        err.to_string()
            .contains("another migrator holds the database"),
        "{err}"
    );
    assert_eq!(err.driver().and_then(Error::sqlstate), Some("55P03"));
    holder
        .run_raw(|raw| raw.execute_batch("ROLLBACK"))
        .await??;

    // with the table in place, the wait is for the first migration
    run(&conn, &config(), &all[..1], PlanOptions::default(), &LIVE).await?;
    holder
        .run_raw(|raw| raw.execute_batch("BEGIN IMMEDIATE"))
        .await??;
    let err = run(&conn, &config(), &all, PlanOptions::default(), &LIVE)
        .await
        .err()
        .ok_or("the lock is held")?;
    assert!(matches!(err, MigrateError::Busy(_)), "{err:?}");
    // nothing was left half done, and a free lock lets the same migrator carry on
    assert!(conn.is_autocommit().await?);
    assert_eq!(history(&holder).await?, [(1, true)]);
    holder
        .run_raw(|raw| raw.execute_batch("ROLLBACK"))
        .await??;
    assert_eq!(
        versions(run(&conn, &config(), &all, PlanOptions::default(), &LIVE).await?),
        [2]
    );
    Ok(())
}

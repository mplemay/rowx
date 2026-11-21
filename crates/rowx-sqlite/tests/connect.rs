// pub, so the shared helpers are reachable from the crate root and need no visibility waiver
pub mod common;

#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;
use std::time::{Duration, Instant};

use common::{TestResult, connect, failure, fetch, file, file_options, memory, path, pragma, run};
use rowx_sqlite::conn::ConnectOptions;
use rowx_sqlite::dsn::{OpenMode, Target};
use rowx_sqlite::error::Error;
use rusqlite::config::DbConfig;

#[tokio::test]
async fn memory_connection_applies_every_pragma() -> TestResult {
    let conn = connect(&memory()).await?;
    assert_eq!(conn.journal_mode(), "memory");
    assert_eq!(pragma(&conn, "foreign_keys").await?, 1);
    // FULL
    assert_eq!(pragma(&conn, "synchronous").await?, 2);
    assert_eq!(pragma(&conn, "recursive_triggers").await?, 0);
    assert_eq!(pragma(&conn, "busy_timeout").await?, 5000);
    assert_eq!(pragma(&conn, "query_only").await?, 0);
    assert!(conn.variable_limit() > 0);
    Ok(())
}

#[tokio::test]
async fn a_file_connection_uses_wal() -> TestResult {
    let dir = tempfile::tempdir()?;
    let conn = connect(&file(&dir)).await?;
    assert_eq!(conn.journal_mode(), "wal");
    let rows = fetch(&conn, "PRAGMA journal_mode").await?.rows;
    assert_eq!(rows.len(), 1);
    assert_eq!(pragma(&conn, "foreign_keys").await?, 1);
    assert_eq!(pragma(&conn, "synchronous").await?, 2);
    assert_eq!(pragma(&conn, "recursive_triggers").await?, 0);
    Ok(())
}

#[tokio::test]
async fn busy_timeout_follows_the_option() -> TestResult {
    let mut options = memory();
    options.busy_timeout = Duration::from_millis(1234);
    let conn = connect(&options).await?;
    assert_eq!(pragma(&conn, "busy_timeout").await?, 1234);
    Ok(())
}

#[tokio::test]
async fn the_variable_limit_is_read_at_connect() -> TestResult {
    let conn = connect(&memory()).await?;
    let limit = conn
        .run_raw(|raw| raw.limit(rusqlite::limits::Limit::SQLITE_LIMIT_VARIABLE_NUMBER))
        .await??;
    assert_eq!(i32::try_from(conn.variable_limit())?, limit);
    Ok(())
}

#[tokio::test]
async fn foreign_keys_are_enforced() -> TestResult {
    let conn = connect(&memory()).await?;
    run(&conn, "CREATE TABLE p (id INTEGER PRIMARY KEY)").await?;
    run(&conn, "CREATE TABLE c (p INTEGER REFERENCES p (id))").await?;
    let err = run(&conn, "INSERT INTO c VALUES (1)")
        .await
        .err()
        .ok_or("the insert should fail")?;
    assert_eq!(
        failure(&err),
        Some(("SQLITE_CONSTRAINT_FOREIGNKEY", "23503"))
    );
    Ok(())
}

#[tokio::test]
async fn read_only_opens_read_only_with_query_only() -> TestResult {
    let dir = tempfile::tempdir()?;
    let mut writer = connect(&file(&dir)).await?;
    run(&writer, "CREATE TABLE t (a INTEGER)").await?;
    writer.close().await;

    let mut options = file_options(&dir, "test.db", OpenMode::ReadWrite);
    options.read_only = true;
    let conn = connect(&options).await?;
    assert_eq!(pragma(&conn, "query_only").await?, 1);
    // the WAL file is read, never switched
    assert_eq!(conn.journal_mode(), "wal");
    assert_eq!(fetch(&conn, "SELECT * FROM t").await?.rows.len(), 0);
    let err = run(&conn, "INSERT INTO t VALUES (1)")
        .await
        .err()
        .ok_or("a write should fail")?;
    assert_eq!(failure(&err), Some(("SQLITE_READONLY", "25006")));
    Ok(())
}

#[tokio::test]
async fn a_read_only_open_of_a_non_wal_file_takes_the_mode_it_finds() -> TestResult {
    let dir = tempfile::tempdir()?;
    {
        let raw = rusqlite::Connection::open(path(&dir, "plain.db"))?;
        raw.execute_batch("CREATE TABLE t (a)")?;
    }
    let mut options = file_options(&dir, "plain.db", OpenMode::ReadOnly);
    let conn = connect(&options).await?;
    assert_eq!(conn.journal_mode(), "delete");
    // `read_only` over a read-write mode also opens the file read-only and does not switch
    options = file_options(&dir, "plain.db", OpenMode::ReadWrite);
    options.read_only = true;
    let conn = connect(&options).await?;
    assert_eq!(conn.journal_mode(), "delete");
    Ok(())
}

#[cfg(unix)]
#[tokio::test]
async fn a_failed_switch_to_wal_is_an_error() -> TestResult {
    let dir = tempfile::tempdir()?;
    let target = path(&dir, "locked.db");
    {
        let raw = rusqlite::Connection::open(&target)?;
        raw.execute_batch("CREATE TABLE t (a)")?;
    }
    // SQLite opens a write-protected file read-only even when asked for read-write, and then cannot
    // switch its journal mode
    std::fs::set_permissions(&target, std::fs::Permissions::from_mode(0o444))?;
    // root ignores the mode bits and SQLite then opens the file read-write, so there is nothing to test
    if std::fs::OpenOptions::new()
        .write(true)
        .open(&target)
        .is_ok()
    {
        return Ok(());
    }
    let options = file_options(&dir, "locked.db", OpenMode::ReadWrite);
    let err = connect(&options)
        .await
        .err()
        .ok_or("the switch should fail")?;
    assert!(matches!(
        err.downcast_ref::<Error>().and_then(failure),
        Some(("SQLITE_READONLY", "25006"))
    ));
    Ok(())
}

#[tokio::test]
async fn a_missing_file_is_cant_open() -> TestResult {
    let dir = tempfile::tempdir()?;
    let options = file_options(&dir, "absent.db", OpenMode::ReadWrite);
    let err = connect(&options)
        .await
        .err()
        .ok_or("opening a missing file should fail")?;
    assert!(matches!(
        err.downcast_ref::<Error>().and_then(failure),
        Some(("SQLITE_CANTOPEN", "58030"))
    ));
    Ok(())
}

#[tokio::test]
async fn a_double_quoted_string_is_an_error() -> TestResult {
    let conn = connect(&memory()).await?;
    run(&conn, "CREATE TABLE t (a INTEGER)").await?;
    let err = fetch(&conn, "SELECT \"colum\" FROM t")
        .await
        .err()
        .ok_or("a missing double-quoted column should fail")?;
    assert!(matches!(&err, Error::Sqlite { message, .. } if message.contains("no such column")));
    // DDL too: an index on a double-quoted name that is no column is not an index on a string
    let err = run(&conn, "CREATE INDEX i ON t (\"colum\")")
        .await
        .err()
        .ok_or("an index on a missing double-quoted column should fail")?;
    assert!(matches!(err, Error::Sqlite { .. }));
    let flags = conn
        .run_raw(|raw| {
            Ok::<_, rusqlite::Error>((
                raw.db_config(DbConfig::SQLITE_DBCONFIG_DQS_DML)?,
                raw.db_config(DbConfig::SQLITE_DBCONFIG_DQS_DDL)?,
            ))
        })
        .await??;
    assert_eq!(flags, (false, false));
    Ok(())
}

#[tokio::test]
async fn an_untrusted_connection_distrusts_the_schema_and_is_defensive() -> TestResult {
    let mut options = memory();
    options.untrusted = true;
    let conn = connect(&options).await?;
    assert_eq!(pragma(&conn, "trusted_schema").await?, 0);
    let defensive = conn
        .run_raw(|raw| raw.db_config(DbConfig::SQLITE_DBCONFIG_DEFENSIVE))
        .await??;
    assert!(defensive);

    let trusting = connect(&memory()).await?;
    let defensive = trusting
        .run_raw(|raw| raw.db_config(DbConfig::SQLITE_DBCONFIG_DEFENSIVE))
        .await??;
    assert!(!defensive);
    Ok(())
}

#[tokio::test]
async fn a_second_writer_fails_busy_after_the_busy_timeout() -> TestResult {
    let dir = tempfile::tempdir()?;
    let holder = connect(&file(&dir)).await?;
    run(&holder, "CREATE TABLE t (a INTEGER)").await?;
    run(&holder, "BEGIN IMMEDIATE").await?;

    let mut options = file(&dir);
    options.busy_timeout = Duration::from_millis(400);
    let second = connect(&options).await?;
    let start = Instant::now();
    let err = second
        .simple("BEGIN IMMEDIATE")
        .await
        .err()
        .ok_or("the second writer should fail")?;
    let took = start.elapsed();
    assert_eq!(failure(&err), Some(("SQLITE_BUSY", "55P03")));
    // SQLite's backoff sleeps in steps, so the wait is close to the timeout, not exact
    assert!(took >= Duration::from_millis(300), "{took:?}");
    assert!(took < Duration::from_secs(10), "{took:?}");
    Ok(())
}

#[tokio::test]
async fn close_releases_the_database_before_it_resolves() -> TestResult {
    let dir = tempfile::tempdir()?;
    let mut holder = connect(&file(&dir)).await?;
    run(&holder, "CREATE TABLE t (a INTEGER)").await?;
    run(&holder, "BEGIN IMMEDIATE").await?;
    holder.close().await;
    // idempotent
    holder.close().await;
    assert!(holder.state().is_closed());
    assert!(holder.is_broken());

    let mut options = file(&dir);
    options.busy_timeout = Duration::from_millis(1);
    let next = connect(&options).await?;
    next.simple("BEGIN IMMEDIATE").await?;

    assert!(matches!(
        holder.ping().await,
        Err(Error::Core(rowx_core::error::CoreError::Closed))
    ));
    Ok(())
}

#[tokio::test]
async fn dropping_a_connection_ends_its_worker_and_releases_the_database() -> TestResult {
    let dir = tempfile::tempdir()?;
    let holder = connect(&file(&dir)).await?;
    run(&holder, "CREATE TABLE t (a INTEGER)").await?;
    run(&holder, "BEGIN IMMEDIATE").await?;
    drop(holder);

    // the worker closes the database on its own way out, which rolls the transaction back; the
    // default 5 s busy timeout is only the hang guard here
    let next = connect(&file(&dir)).await?;
    next.simple("BEGIN IMMEDIATE").await?;
    Ok(())
}

#[tokio::test]
async fn loading_extensions_stays_disabled() -> TestResult {
    let conn = connect(&memory()).await?;
    let err = fetch(&conn, "SELECT load_extension('x')")
        .await
        .err()
        .ok_or("extension loading must be disabled")?;
    assert!(
        matches!(&err, Error::Sqlite { message, .. } if message.contains("not authorized")),
        "{err:?}"
    );
    Ok(())
}

#[tokio::test]
async fn the_checker_options_open_a_read_only_untrusted_session() -> TestResult {
    let dir = tempfile::tempdir()?;
    let writer = connect(&file_options(&dir, "check.db", OpenMode::ReadWriteCreate)).await?;
    run(&writer, "CREATE TABLE t (a INTEGER NOT NULL)").await?;
    let limit = Duration::from_secs(3);
    let options = ConnectOptions::checker(Target::File(path(&dir, "check.db")), Some(limit));
    assert_eq!(options.open.mode, OpenMode::ReadOnly);
    assert!(options.read_only && options.untrusted);
    assert_eq!(options.statement_timeout, Some(limit));
    let checker = connect(&options).await?;
    let err = run(&checker, "INSERT INTO t VALUES (1)").await.err();
    assert!(
        matches!(&err, Some(Error::Sqlite { name, .. }) if name.starts_with("SQLITE_READONLY")),
        "{err:?}"
    );
    assert_eq!(pragma(&checker, "query_only").await?, 1);
    assert_eq!(pragma(&checker, "trusted_schema").await?, 0);
    Ok(())
}

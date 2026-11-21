// pub, so the shared helpers are reachable from the crate root and need no visibility waiver
pub mod common;

use std::sync::Arc;
use std::time::{Duration, Instant};

use common::{
    LONG_QUERY, TestResult, backend_of, cancel_of, connect, failure, fetch, file, ints, memory,
    path, run, wait_until, within,
};
use rowx_core::cancel::Backend;
use rowx_core::error::TaskStopped;
use rowx_core::state::Phase;
use rowx_sqlite::cancel::{Hook, spawn};
use rowx_sqlite::error::Error;
use rowx_sqlite::exec::Param;

#[tokio::test]
async fn interrupting_a_long_query_leaves_the_connection_usable_and_leaks_nothing() -> TestResult {
    let conn = Arc::new(connect(&memory()).await?);
    let cancel = cancel_of(&conn);
    let hook = Hook::new(cancel.state.claim()?);
    let worker = Arc::clone(&conn);
    let pending = spawn(
        &cancel,
        &hook,
        async move { fetch(&worker, LONG_QUERY).await },
    )?;

    // the future is dropped, which cancels the operation: the interrupt is resent until the task ends
    drop(pending);
    within(cancel.state.wait_settled()).await??;
    assert_eq!(cancel.state.phase(), Phase::Idle);

    // statements after the cancel run to completion; the interrupt and the flag did not leak
    for n in 1..=5 {
        assert_eq!(ints(&conn, &format!("SELECT {n}")).await?, [n]);
    }
    conn.ping().await?;
    Ok(())
}

#[tokio::test]
async fn a_cancelled_operation_ends_with_an_interrupt() -> TestResult {
    let conn = Arc::new(connect(&memory()).await?);
    let cancel = cancel_of(&conn);
    let hook = Hook::new(cancel.state.claim()?);
    let worker = Arc::clone(&conn);
    let pending = spawn(
        &cancel,
        &hook,
        async move { fetch(&worker, LONG_QUERY).await },
    )?;
    hook.fire();
    // the task is never detached: its result is still delivered, and it is the interrupt
    let outcome = within(pending.wait()).await??;
    let err = outcome.err().ok_or("the query should be interrupted")?;
    assert_eq!(failure(&err), Some(("SQLITE_INTERRUPT", "57014")));
    within(cancel.state.wait_settled()).await??;
    Ok(())
}

#[tokio::test]
async fn a_cancel_before_the_statement_starts_stepping_is_honoured() -> TestResult {
    let conn = connect(&memory()).await?;
    let backend = backend_of(&conn);
    // no statement runs, so the SQLite interrupt does nothing; the flag is what the worker polls
    backend.send_cancel().await?;
    let err = fetch(&conn, "SELECT 1")
        .await
        .err()
        .ok_or("the flag should stop the statement")?;
    assert_eq!(failure(&err), Some(("SQLITE_INTERRUPT", "57014")));
    let err = run(&conn, "CREATE TABLE t (a)")
        .await
        .err()
        .ok_or("the flag should stop the statement")?;
    assert_eq!(failure(&err), Some(("SQLITE_INTERRUPT", "57014")));
    assert!(conn.is_autocommit().await?);

    // `is_alive` runs after the join in `settle` and takes the flag, so the next operation is clean
    assert!(backend.is_alive().await);
    assert_eq!(ints(&conn, "SELECT 2").await?, [2]);
    Ok(())
}

#[tokio::test]
async fn an_interrupt_with_nothing_running_does_nothing() -> TestResult {
    let conn = connect(&memory()).await?;
    // the raw interrupt is connection-wide and a no-op while idle
    conn.run_raw(|raw| raw.get_interrupt_handle().interrupt())
        .await?;
    assert_eq!(ints(&conn, "SELECT 3").await?, [3]);
    Ok(())
}

// rows of about a kilobyte each, so the write outgrows the page cache and spills into the WAL file
const LONG_WRITE: &str = "INSERT INTO t WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 1000000000000) SELECT n, zeroblob(1000) FROM c";

#[tokio::test]
async fn a_cancel_during_a_write_in_a_transaction_rolls_the_transaction_back() -> TestResult {
    let dir = tempfile::tempdir()?;
    let conn = Arc::new(connect(&file(&dir)).await?);
    run(&conn, "CREATE TABLE t (n INTEGER, pad BLOB)").await?;
    conn.simple("BEGIN").await?;
    run(&conn, "INSERT INTO t VALUES (0, NULL)").await?;

    let cancel = cancel_of(&conn);
    let hook = Hook::new(cancel.state.claim()?);
    let worker = Arc::clone(&conn);
    let pending = spawn(&cancel, &hook, async move {
        worker.execute(LONG_WRITE, Vec::<Param>::new()).await
    })?;
    // The write is stepping once its dirty pages no longer fit the page cache and reach the WAL file,
    // which another thread can observe. The cancel therefore lands on a running statement.
    let wal = path(&dir, "test.db-wal");
    wait_until(|| std::fs::metadata(&wal).is_ok_and(|meta| meta.len() > 1_000_000)).await?;
    hook.fire();
    let err = within(pending.wait())
        .await??
        .err()
        .ok_or("the insert should be interrupted")?;
    assert_eq!(failure(&err), Some(("SQLITE_INTERRUPT", "57014")));
    within(cancel.state.wait_settled()).await??;

    // SQLite rolled the whole transaction back, so the mark a cancel leaves is cleared
    assert!(conn.is_autocommit().await?);
    assert!(!cancel.state.maybe_in_txn());
    assert_eq!(ints(&conn, "SELECT count(*) FROM t").await?, [0]);
    run(&conn, "INSERT INTO t VALUES (1, NULL)").await?;
    assert_eq!(ints(&conn, "SELECT count(*) FROM t").await?, [1]);
    Ok(())
}

#[tokio::test]
async fn a_cancel_before_a_write_steps_leaves_the_transaction_open() -> TestResult {
    let conn = connect(&memory()).await?;
    let backend = backend_of(&conn);
    run(&conn, "CREATE TABLE t (n INTEGER)").await?;
    conn.simple("BEGIN").await?;
    run(&conn, "INSERT INTO t VALUES (0)").await?;

    // nothing is running, so only the flag is left for the next statement to find
    backend.send_cancel().await?;
    let err = run(&conn, "INSERT INTO t VALUES (1)")
        .await
        .err()
        .ok_or("the flag should stop the write")?;
    assert_eq!(failure(&err), Some(("SQLITE_INTERRUPT", "57014")));
    // the write never ran, so SQLite did not roll anything back
    assert!(!conn.is_autocommit().await?);
    assert!(backend.is_alive().await);
    assert_eq!(ints(&conn, "SELECT n FROM t").await?, [0]);
    conn.reset(true).await?;
    assert!(conn.is_autocommit().await?);
    Ok(())
}

#[tokio::test]
async fn an_interrupted_read_in_a_transaction_keeps_the_transaction_open() -> TestResult {
    let conn = Arc::new(connect(&memory()).await?);
    conn.simple("BEGIN").await?;
    let cancel = cancel_of(&conn);
    let hook = Hook::new(cancel.state.claim()?);
    let worker = Arc::clone(&conn);
    let pending = spawn(
        &cancel,
        &hook,
        async move { fetch(&worker, LONG_QUERY).await },
    )?;
    hook.fire();
    within(pending.wait()).await??.err().ok_or("interrupted")?;
    within(cancel.state.wait_settled()).await??;

    // only a write rolls the transaction back; a read leaves it open for the pool to reset
    assert!(!conn.is_autocommit().await?);
    assert!(cancel.state.maybe_in_txn());
    conn.reset(true).await?;
    assert!(conn.is_autocommit().await?);
    Ok(())
}

// Observation recorded for #335: `sqlite3_interrupt` does not wake a connection that waits in the
// busy handler. The statement fails with SQLITE_BUSY once `busy_timeout` has run out, not with
// SQLITE_INTERRUPT, so a cancel's latency can reach `busy_timeout`. The interrupts here are raw: the
// cancel flag is never set, so the statement cannot be stopped by its pre-step check instead.
#[tokio::test]
async fn an_interrupt_does_not_wake_the_busy_handler_so_a_cancel_waits_out_the_busy_timeout()
-> TestResult {
    let dir = tempfile::tempdir()?;
    let holder = connect(&file(&dir)).await?;
    run(&holder, "CREATE TABLE t (a INTEGER)").await?;
    run(&holder, "BEGIN IMMEDIATE").await?;

    let busy_timeout = Duration::from_secs(1);
    let mut options = file(&dir);
    options.busy_timeout = busy_timeout;
    let waiter = Arc::new(connect(&options).await?);
    let interrupt = waiter.interrupt_handle();
    let worker = Arc::clone(&waiter);
    let started = Instant::now();
    let mut waiting = tokio::spawn(async move { worker.simple("BEGIN IMMEDIATE").await });

    // interrupt again and again until the statement ends; an interrupt that lands before the statement
    // steps is dropped by SQLite, and one that lands while it waits is ignored by the busy handler
    let outcome = within(async {
        loop {
            tokio::select! {
                joined = &mut waiting => break joined,
                () = tokio::time::sleep(Duration::from_millis(20)) => interrupt.interrupt(),
            }
        }
    })
    .await??;
    let elapsed = started.elapsed();
    let err = outcome.err().ok_or("the write lock is held elsewhere")?;

    assert_eq!(failure(&err), Some(("SQLITE_BUSY", "55P03")), "{err:?}");
    assert!(
        elapsed >= busy_timeout * 9 / 10,
        "the interrupt woke the busy handler after {elapsed:?}"
    );
    // the connection is usable
    assert_eq!(ints(&waiter, "SELECT 1").await?, [1]);
    Ok(())
}

#[tokio::test]
async fn a_worker_panic_surfaces_as_task_stopped_not_a_hang() -> TestResult {
    let conn = Arc::new(connect(&memory()).await?);
    let cancel = cancel_of(&conn);
    let hook = Hook::new(cancel.state.claim()?);
    let worker = Arc::clone(&conn);
    let pending = spawn(&cancel, &hook, async move {
        // the worker unwinds without a panic message or hook, and the reply sender drops with it
        worker
            .run_raw(|_| -> () { std::panic::resume_unwind(Box::new("the worker unwound")) })
            .await
    })?;
    let outcome = within(pending.wait()).await??;
    assert!(
        matches!(outcome, Err(Error::Stopped(TaskStopped))),
        "{outcome:?}"
    );

    // the worker marks the state while it finishes unwinding, which can trail the dropped reply
    wait_until(|| conn.state().phase() == Phase::Broken).await?;
    assert!(conn.is_broken());
    assert!(!backend_of(&conn).is_alive().await);
    // later calls fail at once instead of waiting for a worker that is gone
    assert!(matches!(
        within(conn.ping()).await?,
        Err(Error::Stopped(TaskStopped))
    ));
    assert!(matches!(
        within(fetch(&conn, "SELECT 1")).await?,
        Err(Error::Stopped(TaskStopped))
    ));
    Ok(())
}

#[tokio::test]
async fn a_cancel_that_lands_on_a_dead_worker_settles_broken() -> TestResult {
    let conn = Arc::new(connect(&memory()).await?);
    let cancel = cancel_of(&conn);
    let hook = Hook::new(cancel.state.claim()?);
    let worker = Arc::clone(&conn);
    let pending = spawn(&cancel, &hook, async move {
        let _ = worker
            .run_raw(|_| -> () { std::panic::resume_unwind(Box::new("the worker unwound")) })
            .await;
        std::future::pending::<()>().await;
    })?;
    // the worker is dead (or about to be); the cancel must still settle instead of hanging
    hook.fire();
    drop(pending);
    let settled = within(cancel.state.wait_settled()).await?;
    assert!(settled.is_err());
    assert_eq!(cancel.state.phase(), Phase::Broken);
    Ok(())
}

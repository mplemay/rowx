// pub, so the shared helpers are reachable from the crate root and need no visibility waiver
pub mod common;

use std::time::Duration;

use common::{ENDLESS_ROWS, LONG_QUERY, TestResult, connect, failure, fetch, ints, memory, run};
use rowx_core::row_shape::Shape;
use rowx_sqlite::exec::StreamLimits;

const TIMEOUT: Duration = Duration::from_millis(100);

const fn timed() -> rowx_sqlite::conn::ConnectOptions {
    let mut options = memory();
    options.statement_timeout = Some(TIMEOUT);
    options
}

#[tokio::test]
async fn statement_timeout_interrupts_a_long_statement() -> TestResult {
    let conn = connect(&timed()).await?;
    let err = fetch(&conn, LONG_QUERY)
        .await
        .err()
        .ok_or("the query should time out")?;
    assert_eq!(failure(&err), Some(("SQLITE_INTERRUPT", "57014")));
    Ok(())
}

#[tokio::test]
async fn a_timed_out_statement_does_not_touch_the_following_statements() -> TestResult {
    let conn = connect(&timed()).await?;
    for round in 0..5 {
        let err = fetch(&conn, LONG_QUERY)
            .await
            .err()
            .ok_or("the query should time out")?;
        assert_eq!(
            failure(&err),
            Some(("SQLITE_INTERRUPT", "57014")),
            "{round}"
        );
        // a fast statement right after the timeout runs to completion, and so do the next ones
        assert_eq!(ints(&conn, "SELECT 1").await?, [1]);
        run(&conn, "CREATE TEMP TABLE IF NOT EXISTS t (a)").await?;
        assert_eq!(ints(&conn, "SELECT 2").await?, [2]);
    }
    Ok(())
}

#[tokio::test]
async fn statements_that_finish_in_time_are_not_interrupted() -> TestResult {
    let mut options = memory();
    options.statement_timeout = Some(Duration::from_secs(30));
    let conn = connect(&options).await?;
    for n in 0..50 {
        assert_eq!(ints(&conn, &format!("SELECT {n}")).await?, [n]);
    }
    Ok(())
}

#[tokio::test]
async fn a_stream_whose_first_step_runs_too_long_times_out_at_the_open() -> TestResult {
    let conn = connect(&timed()).await?;
    let err = conn
        .stream(
            LONG_QUERY,
            Vec::new(),
            &Shape::default(),
            StreamLimits::default(),
        )
        .await
        .err()
        .ok_or("the first step should time out")?;
    assert_eq!(failure(&err), Some(("SQLITE_INTERRUPT", "57014")));
    assert_eq!(ints(&conn, "SELECT 3").await?, [3]);
    Ok(())
}

#[tokio::test]
async fn a_stream_waiting_for_its_consumer_is_not_timed_out() -> TestResult {
    let conn = connect(&timed()).await?;
    let mut streaming = conn
        .stream(
            "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 6) SELECT n FROM c",
            Vec::new(),
            &Shape::default(),
            common::limits(1, 1),
        )
        .await?;
    // the worker parks on the full channel with the statement paused, so the clock is stopped. The
    // wait is a negative check: a clock that kept running would interrupt the statement meanwhile,
    // and the pass never depends on the wait being long enough.
    common::wait_until(|| conn.rows_stepped() >= 2).await?;
    tokio::time::sleep(TIMEOUT * 3).await;
    let (sizes, total) = common::drain(&mut streaming.batches).await?;
    assert_eq!((sizes.len(), total), (6, 6));
    Ok(())
}

// an endless stream with a consumer that reads quickly is cut off only by a statement that is itself
// slow, which no batch of this query is
#[tokio::test]
async fn an_endless_stream_with_a_reading_consumer_keeps_going() -> TestResult {
    let conn = connect(&timed()).await?;
    let mut streaming = conn
        .stream(
            ENDLESS_ROWS,
            Vec::new(),
            &Shape::default(),
            common::limits(100, 2),
        )
        .await?;
    for _ in 0..20 {
        streaming.batches.next_batch().await?;
    }
    Ok(())
}

#[tokio::test]
async fn an_interrupted_write_in_an_explicit_transaction_rolls_back_to_autocommit() -> TestResult {
    let conn = connect(&timed()).await?;
    run(&conn, "CREATE TABLE t (n INTEGER)").await?;
    // a conservative mark, as a cancel leaves behind; SQLite's own rollback clears it
    conn.state().mark_maybe_in_txn();
    conn.simple("BEGIN").await?;
    run(&conn, "INSERT INTO t VALUES (0)").await?;
    assert!(!conn.is_autocommit().await?);

    let err = run(
        &conn,
        "INSERT INTO t WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 1000000000000) SELECT n FROM c",
    )
    .await
    .err()
    .ok_or("the write should time out")?;
    assert_eq!(failure(&err), Some(("SQLITE_INTERRUPT", "57014")));

    // the whole transaction was rolled back and the connection is back in autocommit and usable
    assert!(conn.is_autocommit().await?);
    assert!(!conn.state().maybe_in_txn());
    assert_eq!(ints(&conn, "SELECT count(*) FROM t").await?, [0]);
    run(&conn, "INSERT INTO t VALUES (1)").await?;
    assert_eq!(ints(&conn, "SELECT count(*) FROM t").await?, [1]);
    Ok(())
}

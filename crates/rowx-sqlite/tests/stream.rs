// pub, so the shared helpers are reachable from the crate root and need no visibility waiver
pub mod common;

use std::sync::Arc;
use std::time::Duration;

use common::{
    ENDLESS_ROWS, Running, TestResult, connect, drain, failure, ints, limits, memory, start_stream,
    wait_until, within,
};
use rowx_core::row_shape::Shape;
use rowx_core::state::Phase;
use rowx_sqlite::exec::Batch;

const COUNTED_ROWS: &str = "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 1000) SELECT n FROM c";

#[tokio::test]
async fn the_worker_does_not_run_ahead_of_the_channel_bound() -> TestResult {
    let conn = Arc::new(connect(&memory()).await?);
    // 2 rows per batch and 2 batches in the channel: the worker holds a third batch while it waits
    let (batch, buffer) = (2_u64, 2_u64);
    let mut running = start_stream(&conn, COUNTED_ROWS, limits(2, 2)).await?;

    // nothing is consumed, so the worker stops at the bound and cannot pass it
    wait_until(|| conn.rows_stepped() >= (buffer + 1) * batch).await?;
    assert_eq!(conn.rows_stepped(), (buffer + 1) * batch);

    // one batch consumed frees one slot, which lets the worker read exactly one more batch
    assert!(matches!(running.batches.next_batch().await?, Batch::Rows(rows) if rows.len() == 2));
    wait_until(|| conn.rows_stepped() >= (buffer + 2) * batch).await?;
    assert_eq!(conn.rows_stepped(), (buffer + 2) * batch);

    // the rest arrives intact
    let (sizes, total) = within(drain(&mut running.batches)).await??;
    // 1000 rows, two of them in the batch consumed above
    assert_eq!(total, 998);
    assert!(sizes.iter().all(|n| *n == 2));
    assert_eq!(conn.rows_stepped(), 1000);
    Ok(())
}

#[tokio::test]
async fn a_dropped_receiver_stops_a_worker_waiting_on_a_full_channel() -> TestResult {
    let conn = Arc::new(connect(&memory()).await?);
    let running = start_stream(&conn, ENDLESS_ROWS, limits(2, 1)).await?;
    // 2 rows per batch, one batch in the channel, one in the worker's hand
    wait_until(|| conn.rows_stepped() >= 4).await?;
    assert_eq!(conn.rows_stepped(), 4);

    // the receiver goes away while the worker is parked behind a full channel
    drop(running.batches);
    // the producer task ended with the worker, so the claim was released
    within(conn.state().wait_unclaimed()).await?;
    assert_eq!(conn.state().phase(), Phase::Idle);
    assert_eq!(conn.rows_stepped(), 4);
    // the connection works at once
    assert_eq!(ints(&conn, "SELECT 5").await?, [5]);
    Ok(())
}

#[tokio::test]
async fn cancelling_a_stream_whose_receiver_is_alive_and_whose_channel_is_full() -> TestResult {
    let conn = Arc::new(connect(&memory()).await?);
    let mut running = start_stream(&conn, ENDLESS_ROWS, limits(2, 1)).await?;
    wait_until(|| conn.rows_stepped() >= 4).await?;

    // the receiver is alive and holds a full channel; only the cancel can stop the worker
    running.hook.fire();
    within(running.cancel.state.wait_settled()).await??;
    assert_eq!(conn.state().phase(), Phase::Idle);
    assert_eq!(conn.rows_stepped(), 4);

    // the consumer still sees the batch that was already buffered, then that the stream was cut off
    assert!(matches!(
        running.batches.next_batch().await?,
        Batch::Rows(_)
    ));
    // the cancel cut the stream while its channel was full, which is an interrupt, not a dead worker
    let err = running
        .batches
        .next_batch()
        .await
        .err()
        .ok_or("the stream was cut off")?;
    assert_eq!(
        failure(&err),
        Some(("SQLITE_INTERRUPT", "57014")),
        "{err:?}"
    );
    assert!(matches!(
        running.batches.next_batch().await?,
        Batch::End {
            rows_affected: None
        }
    ));

    // the connection is usable and no interrupt leaked into the next statement
    assert_eq!(ints(&conn, "SELECT 6").await?, [6]);
    assert!(conn.state().claim()?.is_some());
    Ok(())
}

// two quick rows, then a step that runs far longer than any test waits
const STALLING_ROWS: &str = "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 1000000000000000) SELECT n FROM c WHERE n <= 2 OR n = 999999999999999";

#[tokio::test]
async fn a_dropped_consumer_future_during_a_stream_with_an_open_cursor_leaves_the_connection_usable()
-> TestResult {
    let conn = Arc::new(connect(&memory()).await?);
    let Running {
        mut batches,
        hook,
        cancel,
    } = start_stream(&conn, STALLING_ROWS, limits(2, 1)).await?;
    wait_until(|| conn.rows_stepped() >= 2).await?;
    assert!(matches!(batches.next_batch().await?, Batch::Rows(rows) if rows.len() == 2));

    // The worker is inside a step that does not return, so the next batch never arrives. The consumer's
    // future is dropped mid-await, as asyncio does when it cancels a task, while the receiver and the
    // open cursor live on. The timeout only drops the future; nothing relies on how long it waits.
    assert!(
        tokio::time::timeout(Duration::from_millis(50), batches.next_batch())
            .await
            .is_err()
    );
    // the claim is still held: only the cancel ends the operation
    assert_eq!(cancel.state.phase(), Phase::Busy);

    hook.fire();
    within(cancel.state.wait_settled()).await??;
    assert_eq!(cancel.state.phase(), Phase::Idle);
    // the receiver is still alive, and the connection is usable
    assert_eq!(ints(&conn, "SELECT 7").await?, [7]);
    let err = batches
        .next_batch()
        .await
        .err()
        .ok_or("the stream was interrupted")?;
    assert_eq!(
        failure(&err),
        Some(("SQLITE_INTERRUPT", "57014")),
        "{err:?}"
    );
    Ok(())
}

#[tokio::test]
async fn closing_with_a_full_stream_and_a_live_receiver_does_not_hang() -> TestResult {
    let mut conn = connect(&memory()).await?;
    let streaming = conn
        .stream(ENDLESS_ROWS, Vec::new(), &Shape::default(), limits(2, 1))
        .await?;
    // the channel is full and the worker is parked on it, with the receiver alive
    wait_until(|| conn.rows_stepped() >= 4).await?;
    tokio::time::timeout(Duration::from_secs(5), conn.close()).await?;
    assert!(conn.is_broken());
    drop(streaming);
    Ok(())
}

#[tokio::test]
async fn closing_while_a_stream_is_stepping_does_not_hang() -> TestResult {
    let mut conn = connect(&memory()).await?;
    let mut streaming = conn
        .stream(ENDLESS_ROWS, Vec::new(), &Shape::default(), limits(100, 2))
        .await?;
    // a consumer that keeps up, so the worker is stepping rather than parked
    let consumer =
        tokio::spawn(async move { while streaming.batches.next_batch().await.is_ok() {} });
    wait_until(|| conn.rows_stepped() >= 1000).await?;
    tokio::time::timeout(Duration::from_secs(5), conn.close()).await?;
    within(consumer).await??;
    assert!(conn.is_broken());
    Ok(())
}

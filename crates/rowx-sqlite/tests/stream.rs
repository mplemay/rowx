// pub, so the shared helpers are reachable from the crate root and need no visibility waiver
pub mod common;

use std::sync::Arc;

use common::{
    ENDLESS_ROWS, Running, TestResult, connect, drain, ints, limits, memory, start_stream,
    wait_until, within,
};
use rowx_core::state::Phase;
use rowx_sqlite::error::Error;
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
    assert!(matches!(
        running.batches.next_batch().await,
        Err(Error::Stopped(_))
    ));
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

#[tokio::test]
async fn a_dropped_consumer_future_during_a_stream_with_an_open_cursor_leaves_the_connection_usable()
-> TestResult {
    let conn = Arc::new(connect(&memory()).await?);
    let Running {
        mut batches,
        hook,
        cancel,
    } = start_stream(&conn, ENDLESS_ROWS, limits(2, 1)).await?;
    wait_until(|| conn.rows_stepped() >= 4).await?;

    // the consumer awaits a batch in its own task, which is then dropped mid-await, as asyncio does
    // when it cancels a task; the cursor is still open on the worker
    let consumer = tokio::spawn(async move {
        let _ = batches.next_batch().await;
        std::future::pending::<()>().await;
    });
    consumer.abort();
    assert!(consumer.await.is_err());
    hook.fire();
    within(cancel.state.wait_settled()).await??;

    assert_eq!(cancel.state.phase(), Phase::Idle);
    assert_eq!(ints(&conn, "SELECT 7").await?, [7]);
    Ok(())
}

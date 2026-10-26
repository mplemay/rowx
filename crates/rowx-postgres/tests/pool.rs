// pub, so the shared helpers are reachable from the crate root and need no visibility waiver
pub mod common;

use std::collections::hash_map::RandomState;
use std::hash::{BuildHasher, Hasher};
use std::sync::Arc;
use std::time::Duration;

use common::TestResult;
use rowx_core::pool::Manage;
use rowx_postgres::cancel::{Hook, spawn};
use rowx_postgres::dsn::ConnectOptions;
use rowx_postgres::error::Error;
use rowx_postgres::pool::{PgManager, Pool, PoolOptions, Pooled, RESET_QUERY};
use tokio::runtime::Handle;

const STRESS_ITERATIONS: u32 = 300;

fn pool_for(dsn: &str, max_size: usize) -> TestResult<Pool> {
    let manager = PgManager::new(
        &ConnectOptions::from_dsn(dsn)?,
        Handle::current(),
        Some(RESET_QUERY.to_owned()),
    )?;
    let options = PoolOptions {
        max_size,
        acquire_timeout: Duration::from_secs(30),
        ..PoolOptions::default()
    };
    Ok(Pool::new(manager, options, Handle::current())?)
}

// runs `sql` on the leased connection the way a binding does: claim, lock, run
async fn query_text(pooled: &Pooled, sql: &str) -> Result<String, Error> {
    let conn = pooled.conn()?;
    let claim = conn
        .cancel
        .state
        .claim_settled_leased(pooled.lease())
        .await?;
    let guard = conn.cancel.backend.conn.lock().await;
    let row = guard.client()?.query_one(sql, &[]).await?;
    drop(guard);
    drop(claim);
    Ok(row.try_get::<_, String>(0)?)
}

fn random_below(n: u64) -> u64 {
    RandomState::new().build_hasher().finish() % n
}

#[tokio::test]
async fn terminated_idle_connection_is_replaced() -> TestResult {
    let Some(schema) = common::fresh_schema().await? else {
        return Ok(());
    };
    let pool = pool_for(&schema.dsn, 1)?;
    let admin = schema.connect().await?;

    let first = pool.acquire().await?;
    let pid = query_text(&first, "SELECT pg_backend_pid()::text").await?;
    let dead = first.conn()?.clone();
    drop(first);

    // with max_size=1 this acquire waits for the release to finish, so the connection is idle
    drop(pool.acquire().await?);
    admin
        .execute("SELECT pg_terminate_backend($1::text::int)", &[&pid])
        .await?;
    // the connection task notices the closed socket shortly after the server drops it
    tokio::time::timeout(Duration::from_secs(10), async {
        while !pool.manager().is_broken(&dead) {
            tokio::task::yield_now().await;
        }
    })
    .await?;

    let next = pool.acquire().await?;
    let next_pid = query_text(&next, "SELECT pg_backend_pid()::text").await?;
    assert_ne!(next_pid, pid);
    drop(next);
    pool.close(Some(Duration::from_secs(5))).await;
    schema.drop().await?;
    Ok(())
}

#[tokio::test]
async fn max_size_one_resets_session() -> TestResult {
    let Some(schema) = common::fresh_schema().await? else {
        return Ok(());
    };
    let pool = pool_for(&schema.dsn, 1)?;

    let first = pool.acquire().await?;
    let pid = query_text(&first, "SELECT pg_backend_pid()::text").await?;
    {
        let conn = first.conn()?;
        let claim = conn
            .cancel
            .state
            .claim_leased(first.lease())?
            .ok_or("settling")?;
        conn.cancel
            .backend
            .conn
            .lock()
            .await
            .simple("BEGIN; SET statement_timeout = '1234ms'; CREATE TABLE reset_probe (i int)")
            .await?;
        // the binding does this for SQL that opens a transaction by hand
        conn.cancel.state.mark_maybe_in_txn();
        drop(claim);
    }
    drop(first);

    let second = pool.acquire().await?;
    assert_eq!(
        query_text(&second, "SELECT pg_backend_pid()::text").await?,
        pid
    );
    assert_eq!(query_text(&second, "SHOW statement_timeout").await?, "0");
    // the table was created inside the open transaction, so it is gone if that was rolled back
    let probe = query_text(
        &second,
        "SELECT coalesce(to_regclass('reset_probe')::text, 'none')",
    )
    .await?;
    assert_eq!(probe, "none");
    drop(second);
    pool.close(Some(Duration::from_secs(5))).await;
    schema.drop().await?;
    Ok(())
}

#[tokio::test]
async fn cancel_stress_300() -> TestResult {
    let Some(schema) = common::fresh_schema().await? else {
        return Ok(());
    };
    let pool = pool_for(&schema.dsn, 1)?;
    let mut failures = Vec::new();

    for n in 0..STRESS_ITERATIONS {
        let pooled = pool.acquire().await?;
        let conn = pooled.conn()?;
        let claim = conn
            .cancel
            .state
            .claim_settled_leased(pooled.lease())
            .await?;
        let inner = Arc::clone(&conn.cancel.backend.conn);
        let hook = Hook::default();
        let pending = spawn(&conn.cancel, claim, &hook, async move {
            let guard = inner.lock().await;
            guard
                .client()?
                .query("SELECT pg_sleep(0.05)", &[])
                .await
                .map(|_| ())
                .map_err(Error::from)
        });
        // cancelled at a random point: before the statement starts, mid-sleep, or after it finished
        let delay = Duration::from_micros(random_below(80_000));
        let _ = tokio::time::timeout(delay, pending.wait()).await;
        drop(pooled);

        // the follow-up goes straight to the pool, which must have settled and reset the connection
        let followup = async {
            let next = pool.acquire().await?;
            query_text(&next, &format!("SELECT {n}::text")).await
        }
        .await;
        match followup {
            Ok(text) if text == n.to_string() => {}
            other => failures.push((n, format!("{other:?}"))),
        }
    }
    assert!(failures.is_empty(), "follow-up failures: {failures:?}");
    pool.close(Some(Duration::from_secs(5))).await;
    schema.drop().await?;
    Ok(())
}

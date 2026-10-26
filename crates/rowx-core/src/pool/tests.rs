use std::future::{Future, ready};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use thiserror::Error;
use tokio::runtime::Handle;
use tokio::time::Instant;

use super::*;
use crate::cancel::Backend;
use crate::state::Phase;

type TestResult<T = ()> = Result<T, Box<dyn std::error::Error>>;

#[derive(Debug, Error)]
#[error("fake failure")]
struct FakeError;

#[derive(Debug, Default)]
struct Shared {
    next_id: AtomicU64,
    attempts: AtomicUsize,
    // connects that still fail
    failing_connects: AtomicUsize,
    fail_reset: AtomicBool,
    pings: AtomicUsize,
    forgets: AtomicUsize,
    resets: Mutex<Vec<(u64, bool)>>,
    closed: Mutex<Vec<u64>>,
}

#[derive(Debug)]
struct Fake(Arc<Shared>);

#[derive(Debug)]
struct FakeConn {
    id: u64,
    state: Arc<State>,
    broken: AtomicBool,
}

#[derive(Debug, Clone)]
struct FakeBackend;

impl Backend for FakeBackend {
    type Error = FakeError;

    fn send_cancel(&self) -> impl Future<Output = Result<(), FakeError>> + Send {
        ready(Ok(()))
    }

    fn is_alive(&self) -> impl Future<Output = bool> + Send {
        ready(true)
    }
}

impl Manage for Fake {
    type Conn = FakeConn;
    type Error = FakeError;
    type Backend = FakeBackend;

    fn connect(&self) -> impl Future<Output = Result<FakeConn, FakeError>> + Send {
        self.0.attempts.fetch_add(1, Ordering::AcqRel);
        let failing = self.0.failing_connects.load(Ordering::Acquire);
        if failing > 0 {
            if failing != usize::MAX {
                self.0
                    .failing_connects
                    .store(failing - 1, Ordering::Release);
            }
            return ready(Err(FakeError));
        }
        ready(Ok(FakeConn {
            id: self.0.next_id.fetch_add(1, Ordering::AcqRel) + 1,
            state: Arc::new(State::default()),
            broken: AtomicBool::new(false),
        }))
    }

    fn state<'a>(&self, conn: &'a FakeConn) -> &'a Arc<State> {
        &conn.state
    }

    fn backend(&self, _conn: &FakeConn) -> FakeBackend {
        FakeBackend
    }

    fn ping(&self, _conn: &FakeConn) -> impl Future<Output = Result<(), FakeError>> + Send {
        self.0.pings.fetch_add(1, Ordering::AcqRel);
        ready(Ok(()))
    }

    fn reset(
        &self,
        conn: &FakeConn,
        rollback: bool,
    ) -> impl Future<Output = Result<(), FakeError>> + Send {
        if self.0.fail_reset.load(Ordering::Acquire) {
            return ready(Err(FakeError));
        }
        lock(&self.0.resets).push((conn.id, rollback));
        ready(Ok(()))
    }

    fn is_broken(&self, conn: &FakeConn) -> bool {
        conn.broken.load(Ordering::Acquire)
            || matches!(conn.state.phase(), Phase::Broken | Phase::Closed)
    }

    async fn settled(&self, conn: &FakeConn) -> bool {
        conn.state.wait_settled().await.is_ok()
    }

    fn forget_statements(&self, _conn: &FakeConn) -> impl Future<Output = ()> + Send {
        self.0.forgets.fetch_add(1, Ordering::AcqRel);
        ready(())
    }

    fn close(&self, conn: FakeConn) -> impl Future<Output = ()> + Send {
        conn.state.close();
        lock(&self.0.closed).push(conn.id);
        ready(())
    }
}

fn options() -> PoolOptions {
    PoolOptions {
        // maintenance only runs when a test advances the clock on purpose
        maintain_interval: Duration::from_secs(3600),
        idle_timeout: None,
        max_lifetime: None,
        ping_after: None,
        ..PoolOptions::default()
    }
}

fn pool(options: PoolOptions) -> TestResult<(Pool<Fake>, Arc<Shared>)> {
    let shared = Arc::new(Shared::default());
    let pool = Pool::new(Fake(Arc::clone(&shared)), options, Handle::current())?;
    Ok((pool, shared))
}

// lets the spawned release, close and maintenance tasks run; paused time advances while all are idle
async fn flush() {
    tokio::time::sleep(Duration::from_millis(1)).await;
}

fn id(pooled: &Pooled<Fake>) -> Result<u64, CoreError> {
    Ok(pooled.conn()?.id)
}

fn closed(shared: &Shared) -> Vec<u64> {
    lock(&shared.closed).clone()
}

#[tokio::test(start_paused = true)]
async fn fifo_waiters() -> TestResult {
    let (pool, _) = pool(PoolOptions {
        max_size: 1,
        ..options()
    })?;
    let first = pool.acquire().await?;
    let order = Arc::new(Mutex::new(Vec::new()));
    let mut tasks = Vec::new();
    for n in 1..=3_u32 {
        let (pool, order) = (pool.clone(), Arc::clone(&order));
        tasks.push(tokio::spawn(async move {
            let held = pool.acquire().await;
            lock(&order).push(n);
            drop(held);
        }));
        // each waiter queues before the next one starts
        flush().await;
    }
    assert_eq!(pool.waiting(), 3);
    drop(first);
    for task in tasks {
        task.await?;
    }
    assert_eq!(*lock(&order), [1, 2, 3]);
    Ok(())
}

#[tokio::test(start_paused = true)]
async fn lifo_idle_reuse() -> TestResult {
    let (pool, _) = pool(options())?;
    let (a, b, c) = (
        pool.acquire().await?,
        pool.acquire().await?,
        pool.acquire().await?,
    );
    let ids = (id(&a)?, id(&b)?, id(&c)?);
    for held in [a, b, c] {
        drop(held);
        flush().await;
    }
    assert_eq!(id(&pool.acquire().await?)?, ids.2);
    // the first drop returned its connection, so it is the next most recent
    let again = pool.acquire().await?;
    let next = pool.acquire().await?;
    assert_eq!((id(&again)?, id(&next)?), (ids.1, ids.0));
    Ok(())
}

#[tokio::test(start_paused = true)]
async fn acquire_timeout() -> TestResult {
    let (pool, _) = pool(PoolOptions {
        max_size: 1,
        acquire_timeout: Duration::from_secs(5),
        ..options()
    })?;
    let _held = pool.acquire().await?;
    let start = Instant::now();
    let err = pool.acquire().await.err();
    assert!(matches!(err, Some(PoolError::Core(CoreError::PoolTimeout))));
    assert!(start.elapsed() >= Duration::from_secs(5));
    assert_eq!(pool.waiting(), 0);
    Ok(())
}

#[tokio::test(start_paused = true)]
async fn max_lifetime_retires() -> TestResult {
    let (pool, shared) = pool(PoolOptions {
        max_lifetime: Some(Duration::from_secs(10)),
        ..options()
    })?;
    let first = id(&pool.acquire().await?)?;
    flush().await;
    tokio::time::advance(Duration::from_secs(11)).await;
    let second = id(&pool.acquire().await?)?;
    assert_ne!(first, second);
    assert_eq!(closed(&shared), [first]);
    Ok(())
}

#[tokio::test(start_paused = true)]
async fn max_queries_retires() -> TestResult {
    let (pool, shared) = pool(PoolOptions {
        max_queries: Some(2),
        ..options()
    })?;
    let first = id(&pool.acquire().await?)?;
    flush().await;
    assert_eq!(id(&pool.acquire().await?)?, first);
    flush().await;
    // two checkouts have completed, so the connection is closed instead of reused
    assert_ne!(id(&pool.acquire().await?)?, first);
    assert_eq!(closed(&shared), [first]);
    Ok(())
}

#[tokio::test(start_paused = true)]
async fn expire_connections() -> TestResult {
    let (pool, shared) = pool(options())?;
    let idle = pool.acquire().await?;
    let busy = pool.acquire().await?;
    let (idle_id, busy_id) = (id(&idle)?, id(&busy)?);
    drop(idle);
    flush().await;
    pool.expire_connections();
    assert_ne!(id(&pool.acquire().await?)?, idle_id);
    drop(busy);
    flush().await;
    let mut gone = closed(&shared);
    gone.sort_unstable();
    assert_eq!(gone, [idle_id, busy_id]);
    Ok(())
}

#[tokio::test(start_paused = true)]
async fn idle_timeout_reaps_above_min() -> TestResult {
    let (pool, shared) = pool(PoolOptions {
        min_size: 1,
        idle_timeout: Some(Duration::from_secs(10)),
        maintain_interval: Duration::from_secs(1),
        ..options()
    })?;
    let held = (
        pool.acquire().await?,
        pool.acquire().await?,
        pool.acquire().await?,
    );
    drop(held);
    flush().await;
    assert_eq!(pool.idle(), 3);
    tokio::time::advance(Duration::from_secs(12)).await;
    flush().await;
    assert_eq!((pool.size(), pool.idle()), (1, 1));
    assert_eq!(closed(&shared).len(), 2);
    Ok(())
}

#[tokio::test(start_paused = true)]
async fn min_size_maintained() -> TestResult {
    let (pool, shared) = pool(PoolOptions {
        min_size: 2,
        maintain_interval: Duration::from_secs(1),
        ..options()
    })?;
    pool.open().await?;
    assert_eq!((pool.size(), pool.idle()), (2, 2));
    let mut held = pool.acquire().await?;
    assert!(held.init_pending());
    held.clear_init_pending();
    assert!(!held.init_pending());
    drop(held);
    flush().await;
    // a connection that dies while idle is replaced in the background
    let dead = pool.acquire().await?;
    dead.conn()?.broken.store(true, Ordering::Release);
    drop(dead);
    flush().await;
    tokio::time::advance(Duration::from_secs(2)).await;
    flush().await;
    assert_eq!((pool.size(), pool.idle()), (2, 2));
    assert_eq!(shared.next_id.load(Ordering::Acquire), 3);
    Ok(())
}

#[tokio::test(start_paused = true)]
async fn close_waits_for_checked_out() -> TestResult {
    let (pool, shared) = pool(PoolOptions {
        max_size: 1,
        ..options()
    })?;
    let held = pool.acquire().await?;
    let held_id = id(&held)?;
    let waiter = {
        let pool = pool.clone();
        tokio::spawn(async move { pool.acquire().await.err() })
    };
    flush().await;
    let closing = {
        let pool = pool.clone();
        tokio::spawn(async move { pool.close(None).await })
    };
    flush().await;
    // waiters fail at once, but close() keeps waiting for the lease holder
    assert!(matches!(
        waiter.await?,
        Some(PoolError::Core(CoreError::PoolClosed))
    ));
    assert!(!closing.is_finished());
    assert!(matches!(
        pool.acquire().await.err(),
        Some(PoolError::Core(CoreError::PoolClosed))
    ));
    drop(held);
    closing.await?;
    assert_eq!(closed(&shared), [held_id]);
    assert_eq!(pool.size(), 0);
    Ok(())
}

#[tokio::test(start_paused = true)]
async fn failed_reset_discards() -> TestResult {
    let (pool, shared) = pool(options())?;
    let first = pool.acquire().await?;
    let first_id = id(&first)?;
    shared.fail_reset.store(true, Ordering::Release);
    drop(first);
    flush().await;
    assert_eq!((pool.size(), pool.idle()), (0, 0));
    assert_eq!(closed(&shared), [first_id]);
    shared.fail_reset.store(false, Ordering::Release);
    assert_ne!(id(&pool.acquire().await?)?, first_id);
    Ok(())
}

#[tokio::test(start_paused = true)]
async fn stale_lease_fails_claim() -> TestResult {
    let (pool, _) = pool(options())?;
    let held = pool.acquire().await?;
    let lease = held.lease();
    let state = Arc::clone(&held.conn()?.state);
    assert!(state.claim_leased(lease).is_ok());
    drop(held);
    flush().await;
    assert_eq!(state.claim_leased(lease).err(), Some(CoreError::StaleLease));
    // the next checkout gets a new lease that does work
    let next = pool.acquire().await?;
    assert_ne!(next.lease(), lease);
    assert!(state.claim_leased(next.lease()).is_ok());
    drop(next);
    Ok(())
}

#[tokio::test(start_paused = true)]
async fn release_of_busy_settles() -> TestResult {
    let (pool, shared) = pool(options())?;
    let held = pool.acquire().await?;
    let held_id = id(&held)?;
    let state = Arc::clone(&held.conn()?.state);
    let claim = state.claim_leased(held.lease())?.ok_or("settling")?;
    // an abandoned operation: the claim is handed off and the connection stays Busy
    claim.hand_off();
    assert_eq!(state.phase(), Phase::Busy);
    drop(held);
    flush().await;
    assert_eq!(state.phase(), Phase::Idle);
    // a cancelled operation may have left a transaction open, so the reset rolls back
    assert_eq!(*lock(&shared.resets), [(held_id, true)]);
    assert_eq!(id(&pool.acquire().await?)?, held_id);
    Ok(())
}

#[tokio::test(start_paused = true)]
async fn reset_rolls_back_only_when_needed() -> TestResult {
    let (pool, shared) = pool(options())?;
    let held = pool.acquire().await?;
    let state = Arc::clone(&held.conn()?.state);
    drop(held);
    flush().await;
    let held = pool.acquire().await?;
    state.mark_maybe_in_txn();
    drop(held);
    flush().await;
    let resets: Vec<bool> = lock(&shared.resets).iter().map(|(_, r)| *r).collect();
    assert_eq!(resets, [false, true]);
    assert!(!state.maybe_in_txn());
    Ok(())
}

#[tokio::test(start_paused = true)]
async fn connect_retries_with_backoff() -> TestResult {
    let (pool, shared) = pool(options())?;
    shared.failing_connects.store(3, Ordering::Release);
    let start = Instant::now();
    let _held = pool.acquire().await?;
    assert_eq!(shared.attempts.load(Ordering::Acquire), 4);
    // 50ms + 100ms + 200ms of backoff
    assert!(start.elapsed() >= Duration::from_millis(350));
    assert!(start.elapsed() < Duration::from_secs(1));
    Ok(())
}

#[tokio::test(start_paused = true)]
async fn acquire_timeout_covers_retries() -> TestResult {
    let (pool, shared) = pool(PoolOptions {
        acquire_timeout: Duration::from_secs(2),
        ..options()
    })?;
    shared.failing_connects.store(usize::MAX, Ordering::Release);
    let start = Instant::now();
    let err = pool.acquire().await.err();
    assert!(matches!(err, Some(PoolError::Core(CoreError::PoolTimeout))));
    assert!(start.elapsed() >= Duration::from_secs(2));
    assert!(start.elapsed() < Duration::from_secs(3));
    assert!(shared.attempts.load(Ordering::Acquire) > 2);
    assert_eq!(pool.size(), 0);
    Ok(())
}

#[tokio::test(start_paused = true)]
async fn schema_epoch_clears_cache() -> TestResult {
    let (pool, shared) = pool(options())?;
    drop(pool.acquire().await?);
    flush().await;
    drop(pool.acquire().await?);
    flush().await;
    assert_eq!(shared.forgets.load(Ordering::Acquire), 0);
    pool.bump_epoch();
    drop(pool.acquire().await?);
    flush().await;
    assert_eq!(shared.forgets.load(Ordering::Acquire), 1);
    // the connection is now current, so the next checkout leaves the cache alone
    drop(pool.acquire().await?);
    flush().await;
    assert_eq!(shared.forgets.load(Ordering::Acquire), 1);
    Ok(())
}

#[tokio::test(start_paused = true)]
async fn close_timeout_terminates() -> TestResult {
    let (pool, shared) = pool(options())?;
    let held = pool.acquire().await?;
    let held_id = id(&held)?;
    let state = Arc::clone(&held.conn()?.state);
    let start = Instant::now();
    pool.close(Some(Duration::from_secs(1))).await;
    assert!(start.elapsed() >= Duration::from_secs(1));
    assert!(state.is_closed());
    // the lease holder's connection is closed for good when it is released
    drop(held);
    flush().await;
    assert_eq!(closed(&shared), [held_id]);
    assert_eq!(pool.size(), 0);
    Ok(())
}

#[tokio::test(start_paused = true)]
async fn terminate_closes_idle_and_checked_out() -> TestResult {
    let (pool, shared) = pool(options())?;
    let held = pool.acquire().await?;
    let idle = pool.acquire().await?;
    let state = Arc::clone(&held.conn()?.state);
    drop(idle);
    flush().await;
    pool.terminate();
    flush().await;
    assert!(pool.closed());
    assert!(state.is_closed());
    assert_eq!(closed(&shared).len(), 1);
    drop(held);
    Ok(())
}

#[tokio::test(start_paused = true)]
async fn broken_connections_are_replaced() -> TestResult {
    let (pool, _) = pool(options())?;
    let first = pool.acquire().await?;
    let first_id = id(&first)?;
    drop(first);
    flush().await;
    // the connection dies while idle
    {
        let idle = lock(&pool.inner.idle);
        idle[0].conn.broken.store(true, Ordering::Release);
    }
    assert_ne!(id(&pool.acquire().await?)?, first_id);
    Ok(())
}

#[tokio::test(start_paused = true)]
async fn ping_runs_only_after_idling() -> TestResult {
    let (pool, shared) = pool(PoolOptions {
        ping_after: Some(Duration::from_secs(30)),
        ..options()
    })?;
    drop(pool.acquire().await?);
    flush().await;
    drop(pool.acquire().await?);
    flush().await;
    assert_eq!(shared.pings.load(Ordering::Acquire), 0);
    tokio::time::advance(Duration::from_secs(31)).await;
    drop(pool.acquire().await?);
    assert_eq!(shared.pings.load(Ordering::Acquire), 1);
    Ok(())
}

#[tokio::test(start_paused = true)]
async fn detach_frees_capacity() -> TestResult {
    let (pool, shared) = pool(PoolOptions {
        max_size: 1,
        ..options()
    })?;
    let conn = pool.acquire().await?.detach()?;
    assert_eq!(pool.size(), 0);
    let next = pool.acquire().await?;
    assert_ne!(id(&next)?, conn.id);
    assert_eq!(closed(&shared).len(), 0);
    Ok(())
}

#[tokio::test(start_paused = true)]
async fn dropping_a_pool_closes_idle_connections() -> TestResult {
    let (pool, shared) = pool(options())?;
    drop(pool.acquire().await?);
    flush().await;
    drop(pool);
    flush().await;
    assert_eq!(closed(&shared).len(), 1);
    Ok(())
}

#[test]
fn option_validation() {
    let ok = PoolOptions::default();
    assert_eq!(ok.validate(), Ok(()));
    let cases = [
        (
            PoolOptions {
                max_size: 0,
                ..PoolOptions::default()
            },
            OptionsError::MaxSize,
        ),
        (
            PoolOptions {
                min_size: 11,
                ..PoolOptions::default()
            },
            OptionsError::MinSize,
        ),
        (
            PoolOptions {
                max_queries: Some(0),
                ..PoolOptions::default()
            },
            OptionsError::MaxQueries,
        ),
        (
            PoolOptions {
                maintain_interval: Duration::ZERO,
                ..PoolOptions::default()
            },
            OptionsError::MaintainInterval,
        ),
    ];
    for (bad, expected) in cases {
        assert_eq!(bad.validate(), Err(expected));
    }
    assert_eq!(
        PoolOptions {
            min_size: 10,
            max_queries: Some(1),
            ..ok
        }
        .validate(),
        Ok(())
    );
}

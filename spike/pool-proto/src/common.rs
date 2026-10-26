use std::ops::DerefMut;
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::Arc;
use tokio::sync::{watch, Notify};
use tokio::time::{Duration, Instant};

#[derive(Clone, Debug)]
pub struct Opts {
    pub min_size: usize,
    pub max_size: usize,
    pub acquire_timeout: Duration,
    pub idle_timeout: Duration,
    pub max_lifetime: Duration,
    pub max_queries: Option<u32>,
}

impl Default for Opts {
    fn default() -> Self {
        Self {
            min_size: 0,
            max_size: 4,
            acquire_timeout: Duration::from_secs(5),
            idle_timeout: Duration::from_secs(60),
            max_lifetime: Duration::from_secs(600),
            max_queries: None,
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum Err {
    Timeout,
    Closed,
    Other,
}

// a fake connection: `settle` models the Settling phase after a cancelled operation
#[derive(Debug)]
pub struct Conn {
    pub id: u64,
    pub born: Instant,
    pub last_used: Instant,
    pub queries: u32,
    pub generation: u64,
    settle: watch::Sender<bool>,
}

impl Conn {
    pub fn new(id: u64, generation: u64) -> Self {
        let now = Instant::now();
        Self { id, born: now, last_used: now, queries: 0, generation, settle: watch::Sender::new(true) }
    }

    pub fn cancel_in_flight(&self, settle_after: Duration) {
        self.settle.send_replace(false);
        let tx = self.settle.clone();
        tokio::spawn(async move {
            tokio::time::sleep(settle_after).await;
            tx.send_replace(true);
        });
    }

    pub async fn wait_settled(&self) {
        let mut rx = self.settle.subscribe();
        let _ = rx.wait_for(|settled| *settled).await;
    }
}

#[derive(Debug, Default)]
pub struct Factory {
    pub next_id: AtomicU64,
    pub generation: AtomicU64,
    pub fail_reset: AtomicBool,
    // the micro-benchmark turns the reset round trip off
    pub no_reset: AtomicBool,
}

impl Factory {
    pub fn make(&self) -> Conn {
        Conn::new(self.next_id.fetch_add(1, Ordering::Relaxed), self.generation.load(Ordering::Relaxed))
    }

    pub async fn reset(&self) -> Result<(), ()> {
        if self.no_reset.load(Ordering::Relaxed) {
            return Ok(());
        }
        // one fake round trip
        tokio::time::sleep(Duration::from_millis(1)).await;
        if self.fail_reset.load(Ordering::Relaxed) { Err(()) } else { Ok(()) }
    }
}

pub fn retired(c: &Conn, o: &Opts, f: &Factory) -> bool {
    c.generation != f.generation.load(Ordering::Relaxed)
        || c.born.elapsed() >= o.max_lifetime
        || o.max_queries.is_some_and(|m| c.queries >= m)
}

// close() waits for checked-out connections: a counter plus a Notify, ~15 lines for either pool
#[derive(Debug, Default)]
pub struct Outstanding {
    n: AtomicUsize,
    notify: Notify,
}

impl Outstanding {
    pub fn inc(&self) {
        self.n.fetch_add(1, Ordering::AcqRel);
    }
    pub fn dec(&self) {
        if self.n.fetch_sub(1, Ordering::AcqRel) == 1 {
            self.notify.notify_waiters();
        }
    }
    pub async fn drained(&self) {
        loop {
            let waiting = self.notify.notified();
            tokio::pin!(waiting);
            waiting.as_mut().enable();
            if self.n.load(Ordering::Acquire) == 0 {
                return;
            }
            waiting.await;
        }
    }
}

pub trait PoolApi: Sized + Send + Sync + 'static {
    type Lease: DerefMut<Target = Conn> + Send + 'static;
    // must be called inside a runtime: the pool stores the Handle for release tasks
    fn new(opts: Opts, f: Arc<Factory>) -> Self;
    fn acquire(&self) -> impl Future<Output = Result<Self::Lease, Err>> + Send;
    fn size(&self) -> usize;
    fn idle(&self) -> usize;
    fn expire(&self);
    // open(): warm to min_size; also what the background loop runs each tick (reap, then fill)
    fn maintain(&self) -> impl Future<Output = ()> + Send;
    fn close(&self) -> impl Future<Output = ()> + Send;
}

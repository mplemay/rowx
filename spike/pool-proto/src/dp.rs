// Prototype A: deadpool::managed with the layers rowx needs on top.
use crate::common::{Conn, Err, Factory, Opts, Outstanding, PoolApi, retired};
use deadpool::managed::{self, Metrics, Object, PoolError, QueueMode, RecycleError, RecycleResult, Timeouts};
use deadpool::Runtime;
use std::ops::{Deref, DerefMut};
use std::sync::atomic::Ordering;
use std::sync::Arc;
use tokio::runtime::Handle;
use tokio::time::Instant;

pub struct Mgr {
    f: Arc<Factory>,
    o: Opts,
}

impl managed::Manager for Mgr {
    type Type = Conn;
    type Error = ();

    async fn create(&self) -> Result<Conn, ()> {
        Ok(self.f.make())
    }

    // runs on the ACQUIRE path (deadpool recycles lazily on get); the reset itself runs in the release task below
    async fn recycle(&self, c: &mut Conn, _: &Metrics) -> RecycleResult<()> {
        c.wait_settled().await;
        if retired(c, &self.o, &self.f) {
            return Err(RecycleError::message("retired"));
        }
        Ok(())
    }
}

pub struct DpPool {
    pool: managed::Pool<Mgr>,
    f: Arc<Factory>,
    o: Opts,
    handle: Handle,
    out: Arc<Outstanding>,
}

pub struct Lease {
    obj: Option<Object<Mgr>>,
    f: Arc<Factory>,
    o: Opts,
    handle: Handle,
    out: Arc<Outstanding>,
}

impl Deref for Lease {
    type Target = Conn;
    fn deref(&self) -> &Conn {
        self.obj.as_deref().expect("live lease")
    }
}
impl DerefMut for Lease {
    fn deref_mut(&mut self) -> &mut Conn {
        self.obj.as_deref_mut().expect("live lease")
    }
}

// release: spawned through the stored Handle, so it works from Drop on any thread. The Object stays checked out
// (counting toward capacity) until the task drops it, which is what returns it to the idle queue.
impl Drop for Lease {
    fn drop(&mut self) {
        let Some(mut obj) = self.obj.take() else { return };
        let (f, o, out) = (self.f.clone(), self.o.clone(), self.out.clone());
        self.handle.spawn(async move {
            obj.wait_settled().await;
            obj.last_used = Instant::now();
            if f.reset().await.is_err() || retired(&obj, &o, &f) {
                drop(Object::take(obj)); // shrink the pool instead of returning it
            } else {
                drop(obj);
            }
            out.dec();
        });
    }
}

impl PoolApi for DpPool {
    type Lease = Lease;

    fn new(o: Opts, f: Arc<Factory>) -> Self {
        let pool = managed::Pool::builder(Mgr { f: f.clone(), o: o.clone() })
            .max_size(o.max_size)
            .queue_mode(QueueMode::Lifo)
            .timeouts(Timeouts { wait: Some(o.acquire_timeout), create: None, recycle: None })
            .runtime(Runtime::Tokio1)
            .build()
            .expect("build");
        Self { pool, f, o, handle: Handle::current(), out: Arc::default() }
    }

    async fn acquire(&self) -> Result<Lease, Err> {
        let mut obj = self.pool.get().await.map_err(|e| match e {
            PoolError::Timeout(_) => Err::Timeout,
            PoolError::Closed => Err::Closed,
            _ => Err::Other,
        })?;
        obj.queries += 1;
        self.out.inc();
        Ok(Lease { obj: Some(obj), f: self.f.clone(), o: self.o.clone(), handle: self.handle.clone(), out: self.out.clone() })
    }

    fn size(&self) -> usize {
        self.pool.status().size
    }

    fn idle(&self) -> usize {
        self.pool.status().available.max(0) as usize
    }

    fn expire(&self) {
        self.f.generation.fetch_add(1, Ordering::Relaxed);
    }

    async fn maintain(&self) {
        let st = self.pool.status();
        let mut must_keep = self.o.min_size.saturating_sub(st.size - self.idle());
        let timeout = self.o.idle_timeout;
        let (o, f) = (&self.o, &self.f);
        self.pool.retain(|c, _| {
            // deadpool recycles lazily, so retired idle connections must be evicted here or get() would eat them
            if retired(c, o, f) {
                return false;
            }
            if must_keep > 0 {
                must_keep -= 1;
                return true;
            }
            c.last_used.elapsed() < timeout
        });
        // warm to min_size: hold the objects together so they are distinct, then return them
        let missing = self.o.min_size.saturating_sub(self.pool.status().size);
        let mut held = Vec::new();
        for _ in 0..missing {
            if let Ok(o) = self.pool.get().await {
                held.push(o);
            }
        }
    }

    async fn close(&self) {
        self.pool.close();
        self.out.drained().await;
    }
}

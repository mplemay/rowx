// Prototype B: our own pool: FIFO-fair semaphore, LIFO idle stack, release task.
use crate::common::{Conn, Err, Factory, Opts, Outstanding, PoolApi, retired};
use std::ops::{Deref, DerefMut};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use tokio::runtime::Handle;
use tokio::sync::{OwnedSemaphorePermit, Semaphore};
use tokio::time::Instant;

struct Inner {
    f: Arc<Factory>,
    o: Opts,
    sem: Arc<Semaphore>, // tokio's semaphore queues waiters FIFO
    idle: Mutex<Vec<Conn>>, // LIFO
    size: AtomicUsize,
    closed: AtomicBool,
    out: Outstanding,
}

pub struct OwnPool {
    inner: Arc<Inner>,
    handle: Handle,
}

pub struct Lease {
    conn: Option<Conn>,
    permit: Option<OwnedSemaphorePermit>,
    inner: Arc<Inner>,
    handle: Handle,
}

impl Deref for Lease {
    type Target = Conn;
    fn deref(&self) -> &Conn {
        self.conn.as_ref().expect("live lease")
    }
}
impl DerefMut for Lease {
    fn deref_mut(&mut self) -> &mut Conn {
        self.conn.as_mut().expect("live lease")
    }
}

impl Drop for Lease {
    fn drop(&mut self) {
        let (Some(mut conn), Some(permit)) = (self.conn.take(), self.permit.take()) else { return };
        let inner = self.inner.clone();
        // the permit rides along, so capacity counts a connection while it settles and resets
        self.handle.spawn(async move {
            conn.wait_settled().await;
            conn.last_used = Instant::now();
            let ok = inner.f.reset().await.is_ok()
                && !retired(&conn, &inner.o, &inner.f)
                && !inner.closed.load(Ordering::Acquire);
            if ok {
                inner.idle.lock().expect("lock").push(conn);
            } else {
                inner.size.fetch_sub(1, Ordering::AcqRel);
            }
            drop(permit);
            inner.out.dec();
        });
    }
}

impl PoolApi for OwnPool {
    type Lease = Lease;

    fn new(o: Opts, f: Arc<Factory>) -> Self {
        let inner = Inner {
            sem: Arc::new(Semaphore::new(o.max_size)),
            f,
            o,
            idle: Mutex::default(),
            size: AtomicUsize::new(0),
            closed: AtomicBool::new(false),
            out: Outstanding::default(),
        };
        Self { inner: Arc::new(inner), handle: Handle::current() }
    }

    async fn acquire(&self) -> Result<Lease, Err> {
        let i = &self.inner;
        let permit = match tokio::time::timeout(i.o.acquire_timeout, i.sem.clone().acquire_owned()).await {
            Err(_) => return Err(Err::Timeout),
            Ok(Err(_)) => return Err(Err::Closed),
            Ok(Ok(p)) => p,
        };
        let conn = loop {
            let popped = i.idle.lock().expect("lock").pop();
            match popped {
                Some(c) if retired(&c, &i.o, &i.f) => {
                    i.size.fetch_sub(1, Ordering::AcqRel);
                }
                Some(c) => break c,
                None => {
                    i.size.fetch_add(1, Ordering::AcqRel);
                    break i.f.make();
                }
            }
        };
        let mut lease = Lease { conn: Some(conn), permit: Some(permit), inner: self.inner.clone(), handle: self.handle.clone() };
        lease.queries += 1;
        i.out.inc();
        Ok(lease)
    }

    fn size(&self) -> usize {
        self.inner.size.load(Ordering::Acquire)
    }

    fn idle(&self) -> usize {
        self.inner.idle.lock().expect("lock").len()
    }

    fn expire(&self) {
        self.inner.f.generation.fetch_add(1, Ordering::Relaxed);
    }

    async fn maintain(&self) {
        let i = &self.inner;
        {
            let mut idle = i.idle.lock().expect("lock");
            let mut size = i.size.load(Ordering::Acquire);
            // oldest first (the bottom of the stack), so hot connections stay
            let mut k = 0;
            while k < idle.len() && size > i.o.min_size {
                if idle[k].last_used.elapsed() >= i.o.idle_timeout {
                    idle.remove(k);
                    size -= 1;
                } else {
                    k += 1;
                }
            }
            i.size.store(size, Ordering::Release);
        }
        while i.size.load(Ordering::Acquire) < i.o.min_size {
            i.size.fetch_add(1, Ordering::AcqRel);
            let c = i.f.make();
            i.idle.lock().expect("lock").insert(0, c);
        }
    }

    async fn close(&self) {
        self.inner.closed.store(true, Ordering::Release);
        self.inner.sem.close(); // fails waiters at once
        let n = {
            let mut idle = self.inner.idle.lock().expect("lock");
            let n = idle.len();
            idle.clear();
            n
        };
        self.inner.size.fetch_sub(n, Ordering::AcqRel);
        self.inner.out.drained().await;
    }
}

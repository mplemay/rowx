//! A connection pool generic over a backend's [`Manage`] implementation.
//!
//! Waiters queue on a fair semaphore (FIFO) and idle connections form a stack (LIFO). Every
//! connection that is checked out, resetting or settling holds a permit, so capacity counts them.

mod maintain;
mod manage;
mod options;
mod release;
#[cfg(test)]
mod tests;

use std::collections::HashMap;
use std::fmt;
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::Duration;

use thiserror::Error;
use tokio::runtime::Handle;
use tokio::sync::{Notify, OwnedSemaphorePermit, Semaphore};
use tokio::task::JoinHandle;
use tokio::time::Instant;

use crate::cancel::Backend;
use crate::error::CoreError;
use crate::state::Phase;

pub use manage::Manage;
pub use options::{OptionsError, PoolOptions};

const FIRST_CONNECT_RETRY: Duration = Duration::from_millis(50);
const MAX_CONNECT_RETRY: Duration = Duration::from_secs(1);

#[derive(Debug, Error)]
pub enum PoolError<E> {
    #[error(transparent)]
    Core(#[from] CoreError),
    #[error(transparent)]
    Manage(E),
}

type Terminate = Arc<dyn Fn() + Send + Sync>;

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

#[derive(Debug)]
struct Meta {
    id: u64,
    created: Instant,
    idle_since: Instant,
    queries: u64,
    expire_gen: u64,
    epoch: u64,
    init_pending: bool,
}

struct Parts<M: Manage> {
    conn: M::Conn,
    meta: Meta,
}

// a permit that wakes `close()` when it goes back, so a waiter sees every release
struct Slot {
    permit: Option<OwnedSemaphorePermit>,
    drained: Arc<Notify>,
}

impl Drop for Slot {
    fn drop(&mut self) {
        drop(self.permit.take());
        self.drained.notify_waiters();
    }
}

struct Inner<M: Manage> {
    manager: Arc<M>,
    options: PoolOptions,
    handle: Handle,
    pid: u32,
    semaphore: Arc<Semaphore>,
    drained: Arc<Notify>,
    // oldest first, so `pop` reuses the most recently returned connection
    idle: Mutex<Vec<Parts<M>>>,
    // every connection, idle or not
    size: AtomicUsize,
    waiting: AtomicUsize,
    closed: AtomicBool,
    next_id: AtomicU64,
    epoch: AtomicU64,
    expire_gen: AtomicU64,
    // the states of checked-out connections, so `terminate` can close what is still in use
    live: Mutex<HashMap<u64, Terminate>>,
    task: Mutex<Option<JoinHandle<()>>>,
}

impl<M: Manage> Drop for Inner<M> {
    fn drop(&mut self) {
        let task = lock(&self.task).take();
        if let Some(task) = task {
            task.abort();
        }
        let idle = std::mem::take(&mut *lock(&self.idle));
        if idle.is_empty() {
            return;
        }
        let manager = Arc::clone(&self.manager);
        // the pool may be dropped outside a runtime, so the stored handle runs the closes
        self.handle.spawn(async move {
            for parts in idle {
                manager.close(parts.conn).await;
            }
        });
    }
}

struct WaitGuard<'a>(&'a AtomicUsize);

impl<'a> WaitGuard<'a> {
    fn new(counter: &'a AtomicUsize) -> Self {
        counter.fetch_add(1, Ordering::AcqRel);
        Self(counter)
    }
}

impl Drop for WaitGuard<'_> {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::AcqRel);
    }
}

impl<M: Manage> Inner<M> {
    fn closed(&self) -> bool {
        self.closed.load(Ordering::Acquire)
    }

    fn check_process(&self) -> Result<(), CoreError> {
        if std::process::id() == self.pid {
            Ok(())
        } else {
            Err(CoreError::Forked)
        }
    }

    fn meta(&self, init_pending: bool) -> Meta {
        let now = Instant::now();
        Meta {
            id: self.next_id.fetch_add(1, Ordering::AcqRel),
            created: now,
            idle_since: now,
            queries: 0,
            expire_gen: self.expire_gen.load(Ordering::Acquire),
            epoch: self.epoch.load(Ordering::Acquire),
            init_pending,
        }
    }

    fn retired(&self, meta: &Meta, now: Instant) -> bool {
        self.options
            .max_lifetime
            .is_some_and(|limit| now.duration_since(meta.created) >= limit)
            || self
                .options
                .max_queries
                .is_some_and(|limit| meta.queries >= limit)
            || meta.expire_gen != self.expire_gen.load(Ordering::Acquire)
    }

    fn pop_idle(&self) -> Option<Parts<M>> {
        lock(&self.idle).pop()
    }

    // false when the pool closed first, so the caller closes the connection itself
    fn push_idle(&self, parts: Parts<M>) -> Result<(), Parts<M>> {
        let mut idle = lock(&self.idle);
        // checked under the lock, so `close` (which sets the flag, then drains) cannot miss this push
        if self.closed() {
            drop(idle);
            return Err(parts);
        }
        idle.push(parts);
        drop(idle);
        Ok(())
    }

    async fn discard(&self, conn: M::Conn) {
        self.manager.close(conn).await;
        self.size.fetch_sub(1, Ordering::AcqRel);
    }

    // what `terminate` does to a checked-out connection: close it for its holder and stop whatever
    // is running, so a long query does not keep its locks
    fn register(&self, id: u64, conn: &M::Conn) {
        let state = Arc::clone(self.manager.state(conn));
        let backend = self.manager.backend(conn);
        let handle = self.handle.clone();
        let terminate: Terminate = Arc::new(move || {
            let running = state.phase() != Phase::Idle;
            state.close();
            if running {
                let backend = backend.clone();
                handle.spawn(async move {
                    // a failed request is fine: closing the connection ends the query anyway
                    let _ = backend.send_cancel().await;
                });
            }
        });
        lock(&self.live).insert(id, terminate);
    }

    fn unregister(&self, id: u64) {
        lock(&self.live).remove(&id);
    }

    fn start(self: &Arc<Self>) {
        let mut task = lock(&self.task);
        if task.is_some() || self.closed() {
            return;
        }
        *task = Some(self.handle.spawn(maintain::run(Arc::downgrade(self))));
    }

    async fn take_permit(&self, deadline: Instant) -> Result<OwnedSemaphorePermit, CoreError> {
        let _waiting = WaitGuard::new(&self.waiting);
        match tokio::time::timeout_at(deadline, Arc::clone(&self.semaphore).acquire_owned()).await {
            Err(_) => Err(CoreError::PoolTimeout),
            Ok(Err(_)) => Err(CoreError::PoolClosed),
            Ok(Ok(permit)) => Ok(permit),
        }
    }

    fn slot(&self, permit: OwnedSemaphorePermit) -> Slot {
        Slot {
            permit: Some(permit),
            drained: Arc::clone(&self.drained),
        }
    }

    // retried with backoff until `deadline`, and each attempt is bounded by `connect_timeout`
    async fn connect_until(&self, deadline: Instant) -> Result<Parts<M>, CoreError> {
        let mut pause = FIRST_CONNECT_RETRY;
        loop {
            let now = Instant::now();
            if now >= deadline {
                return Err(CoreError::PoolTimeout);
            }
            let remaining = deadline - now;
            let limit = self
                .options
                .connect_timeout
                .map_or(remaining, |each| each.min(remaining));
            if let Ok(Ok(conn)) = tokio::time::timeout(limit, self.manager.connect()).await {
                self.size.fetch_add(1, Ordering::AcqRel);
                return Ok(Parts {
                    conn,
                    meta: self.meta(false),
                });
            }
            tokio::time::sleep_until((Instant::now() + pause).min(deadline)).await;
            pause = (pause * 2).min(MAX_CONNECT_RETRY);
        }
    }

    // one attempt, for warming the pool and background maintenance
    async fn connect_once(&self) -> Result<Parts<M>, PoolError<M::Error>> {
        let attempt = self.manager.connect();
        let conn = match self.options.connect_timeout {
            Some(limit) => tokio::time::timeout(limit, attempt)
                .await
                .map_err(|_| CoreError::PoolTimeout)?,
            None => attempt.await,
        }
        .map_err(PoolError::Manage)?;
        Ok(Parts {
            conn,
            meta: self.meta(true),
        })
    }

    // checks an idle connection before it is handed out; false means it must be discarded
    async fn usable(&self, parts: &mut Parts<M>) -> bool {
        let now = Instant::now();
        if self.manager.is_broken(&parts.conn) || self.retired(&parts.meta, now) {
            return false;
        }
        let epoch = self.epoch.load(Ordering::Acquire);
        if parts.meta.epoch != epoch {
            self.manager.forget_statements(&parts.conn).await;
            parts.meta.epoch = epoch;
        }
        let stale = self
            .options
            .ping_after
            .is_some_and(|after| now.duration_since(parts.meta.idle_since) >= after);
        !(stale && self.manager.ping(&parts.conn).await.is_err())
    }

    fn begin_close(&self) {
        self.closed.store(true, Ordering::Release);
        self.semaphore.close();
        let task = lock(&self.task).take();
        if let Some(task) = task {
            task.abort();
        }
    }

    fn terminate_checked_out(&self) {
        let all: Vec<Terminate> = lock(&self.live).values().cloned().collect();
        for terminate in all {
            terminate();
        }
    }

    async fn wait_drained(&self) {
        loop {
            let notified = self.drained.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            if self.semaphore.available_permits() >= self.options.max_size {
                return;
            }
            notified.await;
        }
    }

    // adds one to `size` unless it has reached `limit`, in one step
    fn count_below(&self, limit: usize) -> bool {
        let mut size = self.size.load(Ordering::Acquire);
        while size < limit {
            match self.size.compare_exchange_weak(
                size,
                size + 1,
                Ordering::AcqRel,
                Ordering::Acquire,
            ) {
                Ok(_) => return true,
                Err(actual) => size = actual,
            }
        }
        false
    }

    // opens connections until `min_size` is reached, as far as free permits allow; each connection is
    // counted before it is made, so overlapping fills cannot overshoot
    async fn fill(self: &Arc<Self>) -> Result<(), PoolError<M::Error>> {
        let min = self.options.min_size;
        while !self.closed() {
            if !self.count_below(min) {
                break;
            }
            let mut reservation = Reservation {
                size: &self.size,
                armed: true,
            };
            let Ok(permit) = Arc::clone(&self.semaphore).try_acquire_owned() else {
                break;
            };
            let _slot = self.slot(permit);
            let parts = self.connect_once().await?;
            reservation.armed = false;
            if let Err(parts) = self.push_idle(parts) {
                self.discard(parts.conn).await;
            }
        }
        Ok(())
    }
}

// a counted-but-unfinished connection; dropped (even by a cancelled future) it gives its count back
struct Reservation<'a> {
    size: &'a AtomicUsize,
    armed: bool,
}

impl Drop for Reservation<'_> {
    fn drop(&mut self) {
        if self.armed {
            self.size.fetch_sub(1, Ordering::AcqRel);
        }
    }
}

/// A pool of connections made by `M`.
pub struct Pool<M: Manage> {
    inner: Arc<Inner<M>>,
}

impl<M: Manage> Clone for Pool<M> {
    fn clone(&self) -> Self {
        Self {
            inner: Arc::clone(&self.inner),
        }
    }
}

impl<M: Manage> fmt::Debug for Pool<M> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Pool")
            .field("size", &self.size())
            .field("idle", &self.idle())
            .field("closed", &self.closed())
            .finish_non_exhaustive()
    }
}

impl<M: Manage> Pool<M> {
    /// Does no IO; connections open on `open()` or on the first acquire.
    ///
    /// # Errors
    /// `OptionsError` for a setting that is out of range.
    pub fn new(manager: M, options: PoolOptions, handle: Handle) -> Result<Self, OptionsError> {
        options.validate()?;
        let max_size = options.max_size;
        Ok(Self {
            inner: Arc::new(Inner {
                manager: Arc::new(manager),
                options,
                handle,
                pid: std::process::id(),
                semaphore: Arc::new(Semaphore::new(max_size)),
                drained: Arc::new(Notify::new()),
                idle: Mutex::new(Vec::new()),
                size: AtomicUsize::new(0),
                waiting: AtomicUsize::new(0),
                closed: AtomicBool::new(false),
                next_id: AtomicU64::new(1),
                epoch: AtomicU64::new(0),
                expire_gen: AtomicU64::new(0),
                live: Mutex::new(HashMap::new()),
                task: Mutex::new(None),
            }),
        })
    }

    /// Warms the pool to `min_size` and starts the background maintenance. Safe to call twice.
    ///
    /// # Errors
    /// `Forked`/`PoolClosed` for an unusable pool, or the first connect error.
    pub async fn open(&self) -> Result<(), PoolError<M::Error>> {
        self.inner.check_process()?;
        if self.inner.closed() {
            return Err(CoreError::PoolClosed.into());
        }
        self.inner.start();
        self.inner.fill().await
    }

    /// Hands out an idle connection or opens a new one, waiting its turn behind earlier callers.
    ///
    /// # Errors
    /// `PoolTimeout` after `acquire_timeout`, `PoolClosed` on a closed pool, `Forked` in a child
    /// process.
    pub async fn acquire(&self) -> Result<Pooled<M>, PoolError<M::Error>> {
        let inner = &self.inner;
        inner.check_process()?;
        if inner.closed() {
            return Err(CoreError::PoolClosed.into());
        }
        // the first acquire opens the pool; warming is left to the background task
        inner.start();
        let deadline = Instant::now() + inner.options.acquire_timeout;
        let permit = inner.take_permit(deadline).await?;
        let mut held = Pooled {
            inner: Arc::clone(inner),
            parts: None,
            slot: Some(inner.slot(permit)),
            lease: 0,
            discard_on_drop: true,
        };
        if inner.closed() {
            return Err(CoreError::PoolClosed.into());
        }
        loop {
            let Some(parts) = inner.pop_idle() else {
                held.parts = Some(inner.connect_until(deadline).await?);
                break;
            };
            // owned by the guard from here on, so a cancelled acquire still closes it
            held.parts = Some(parts);
            let usable = match held.parts.as_mut() {
                Some(parts) => matches!(
                    tokio::time::timeout_at(deadline, inner.usable(parts)).await,
                    Ok(true)
                ),
                None => false,
            };
            if usable {
                break;
            }
            if let Some(parts) = held.parts.take() {
                // in the background, so cancelling this acquire cannot lose the count
                let inner = Arc::clone(inner);
                inner.handle.clone().spawn(async move {
                    inner.discard(parts.conn).await;
                });
            }
        }
        let Some(parts) = held.parts.as_ref() else {
            return Err(CoreError::Closed.into());
        };
        held.lease = inner.manager.state(&parts.conn).renew_lease();
        inner.register(parts.meta.id, &parts.conn);
        held.discard_on_drop = false;
        Ok(held)
    }

    /// Stops new acquires, fails waiters at once, waits for checked-out connections (until
    /// `timeout`, then terminates them) and closes every connection.
    pub async fn close(&self, timeout: Option<Duration>) {
        let inner = &self.inner;
        inner.begin_close();
        close_idle(inner).await;
        let drained = inner.wait_drained();
        match timeout {
            None => drained.await,
            Some(limit) => {
                if tokio::time::timeout(limit, drained).await.is_err() {
                    inner.terminate_checked_out();
                }
            }
        }
        close_idle(inner).await;
    }

    /// Closes everything immediately; connections still checked out are closed and discarded on release.
    pub fn terminate(&self) {
        let inner = &self.inner;
        inner.begin_close();
        let idle = std::mem::take(&mut *lock(&inner.idle));
        for parts in idle {
            let inner = Arc::clone(inner);
            self.inner.handle.spawn(async move {
                inner.discard(parts.conn).await;
            });
        }
        inner.terminate_checked_out();
    }

    /// Retires every connection that exists now: idle ones at the next maintenance pass, checked-out
    /// ones when they are released.
    pub fn expire_connections(&self) {
        self.inner.expire_gen.fetch_add(1, Ordering::AcqRel);
    }

    /// A stale-plan error was seen: each connection drops its statement cache at its next checkout.
    pub fn bump_epoch(&self) {
        self.inner.epoch.fetch_add(1, Ordering::AcqRel);
    }

    #[must_use]
    pub fn size(&self) -> usize {
        self.inner.size.load(Ordering::Acquire)
    }

    #[must_use]
    pub fn idle(&self) -> usize {
        lock(&self.inner.idle).len()
    }

    #[must_use]
    pub fn waiting(&self) -> usize {
        self.inner.waiting.load(Ordering::Acquire)
    }

    #[must_use]
    pub fn min_size(&self) -> usize {
        self.inner.options.min_size
    }

    #[must_use]
    pub fn max_size(&self) -> usize {
        self.inner.options.max_size
    }

    #[must_use]
    pub fn closed(&self) -> bool {
        self.inner.closed()
    }

    #[must_use]
    pub fn manager(&self) -> &M {
        &self.inner.manager
    }
}

async fn close_idle<M: Manage>(inner: &Arc<Inner<M>>) {
    let idle = std::mem::take(&mut *lock(&inner.idle));
    for parts in idle {
        inner.discard(parts.conn).await;
    }
}

/// A connection checked out of the pool. Dropping it releases the connection in a background task
/// (settle, reset, back to idle); `release` does the same inline for a caller that drives it.
pub struct Pooled<M: Manage> {
    inner: Arc<Inner<M>>,
    parts: Option<Parts<M>>,
    slot: Option<Slot>,
    lease: u64,
    // set while the pool is still vetting the connection, where a drop must close it instead
    discard_on_drop: bool,
}

impl<M: Manage> fmt::Debug for Pooled<M> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Pooled")
            .field("lease", &self.lease)
            .finish_non_exhaustive()
    }
}

impl<M: Manage> Pooled<M> {
    /// # Errors
    /// `Closed` after the connection was released or detached.
    pub fn conn(&self) -> Result<&M::Conn, CoreError> {
        self.parts
            .as_ref()
            .map(|parts| &parts.conn)
            .ok_or(CoreError::Closed)
    }

    /// The lease to claim the connection with; release revokes it.
    #[must_use]
    pub const fn lease(&self) -> u64 {
        self.lease
    }

    /// `true` for a connection that `open()` or maintenance made and no caller has initialised yet.
    #[must_use]
    pub fn init_pending(&self) -> bool {
        self.parts.as_ref().is_some_and(|p| p.meta.init_pending)
    }

    pub const fn clear_init_pending(&mut self) {
        if let Some(parts) = self.parts.as_mut() {
            parts.meta.init_pending = false;
        }
    }

    /// Releases inline: revokes the lease, settles, resets and returns the connection to idle.
    pub async fn release(mut self) {
        let (parts, slot) = (self.parts.take(), self.slot.take());
        if let (Some(parts), Some(slot)) = (parts, slot) {
            self.inner.release(parts, slot).await;
        }
    }

    /// Closes the connection instead of returning it, for example after a failed hook.
    pub async fn discard(mut self) {
        if let Some(parts) = self.parts.take() {
            self.inner.unregister(parts.meta.id);
            self.inner.discard(parts.conn).await;
        }
        drop(self.slot.take());
    }

    /// Takes the connection out of the pool for good, which frees its capacity.
    ///
    /// # Errors
    /// `Closed` when it was already released.
    pub fn detach(mut self) -> Result<M::Conn, CoreError> {
        let parts = self.parts.take().ok_or(CoreError::Closed)?;
        self.inner.unregister(parts.meta.id);
        self.inner.size.fetch_sub(1, Ordering::AcqRel);
        Ok(parts.conn)
    }
}

impl<M: Manage> Drop for Pooled<M> {
    fn drop(&mut self) {
        let Some(parts) = self.parts.take() else {
            return;
        };
        let slot = self.slot.take();
        let inner = Arc::clone(&self.inner);
        let discard = self.discard_on_drop;
        // this may run outside a runtime (a drop), so the stored handle spawns the task
        self.inner.handle.spawn(async move {
            match slot {
                Some(slot) if !discard => inner.release(parts, slot).await,
                _ => {
                    inner.unregister(parts.meta.id);
                    inner.discard(parts.conn).await;
                }
            }
        });
    }
}

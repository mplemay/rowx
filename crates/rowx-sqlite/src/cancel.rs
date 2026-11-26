use std::fmt;
use std::future::Future;
#[cfg(feature = "test-support")]
use std::sync::atomic::AtomicU64;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex, MutexGuard, PoisonError};
use std::time::Duration;

use rowx_core::cancel::Backend;
use rusqlite::InterruptHandle;
use tokio::sync::Notify;
use tokio::sync::mpsc::{Sender, error::TrySendError};

use crate::error::Error;

pub use rowx_core::cancel::{spawn, spawn_stream};

const CLOSE_INTERRUPT_INTERVAL: Duration = Duration::from_millis(100);

/// What a connection shares with the worker thread, its cancel handle and its timer.
#[derive(Debug)]
pub(crate) struct Shared {
    // set by `send_cancel` and checked by the worker between steps and batches, so a cancel that lands
    // before a statement steps is still honoured; cleared by `is_alive` once the worker has joined
    cancel: AtomicBool,
    alive: AtomicBool,
    #[cfg(feature = "test-support")]
    stepped: AtomicU64,
    // pairs with `wake`: a worker parked on a full stream channel sleeps here
    gate: Mutex<()>,
    wake: Condvar,
    // set while a statement whose outcome the caller must learn runs (a COMMIT); closing then waits for it
    // instead of interrupting it. A lock, not an atomic, so an interrupt is never sent after the check
    spared: Mutex<bool>,
    // wakes async waiters of a cancel request, which the condition variable cannot reach
    requested: Notify,
}

// the gate guards no data, so a poisoned lock is as good as a clean one
fn lock(gate: &Mutex<()>) -> MutexGuard<'_, ()> {
    gate.lock().unwrap_or_else(PoisonError::into_inner)
}

impl Default for Shared {
    fn default() -> Self {
        Self {
            cancel: AtomicBool::new(false),
            alive: AtomicBool::new(true),
            #[cfg(feature = "test-support")]
            stepped: AtomicU64::new(0),
            gate: Mutex::new(()),
            wake: Condvar::new(),
            spared: Mutex::new(false),
            requested: Notify::new(),
        }
    }
}

/// Why a batch did not reach the stream's receiver.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Unsent {
    /// The receiver is gone.
    Closed,
    /// A cancel arrived while the channel was full.
    Cancelled,
}

impl Shared {
    #[must_use]
    pub(crate) fn is_cancelled(&self) -> bool {
        self.cancel.load(Ordering::Acquire)
    }

    pub(crate) fn request_cancel(&self) {
        self.cancel.store(true, Ordering::Release);
        self.wake();
        self.requested.notify_waiters();
    }

    // register, then check, so a request made before the wait began or while it started is not missed
    pub(crate) async fn wait_cancelled(&self) {
        loop {
            let notified = self.requested.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            if self.is_cancelled() {
                return;
            }
            notified.await;
        }
    }

    /// Takes the cancel flag, so a late `send_cancel` cannot leak into the next operation.
    pub(crate) fn clear_cancel(&self) {
        self.cancel.store(false, Ordering::Release);
    }

    #[must_use]
    pub(crate) fn is_alive(&self) -> bool {
        self.alive.load(Ordering::Acquire)
    }

    pub(crate) fn mark_dead(&self) {
        self.alive.store(false, Ordering::Release);
        self.wake();
    }

    #[cfg(feature = "test-support")]
    pub(crate) fn count_row(&self) {
        self.stepped.fetch_add(1, Ordering::Relaxed);
    }

    #[cfg(feature = "test-support")]
    #[must_use]
    pub(crate) fn rows_stepped(&self) -> u64 {
        self.stepped.load(Ordering::Relaxed)
    }

    /// Wakes a worker parked on a full channel. Taking the gate first orders the wake after the
    /// worker's decision to park: the worker holds the gate from its failed `try_send` until the
    /// condition variable releases it, so a slot freed or a flag set just before cannot be missed.
    pub(crate) fn wake(&self) {
        drop(lock(&self.gate));
        self.wake.notify_all();
    }

    /// Sends from the worker thread. A full channel parks the worker on a condition that a cancel and
    /// the receiver's drop both signal, so a worker blocked behind a live receiver still stops.
    ///
    /// # Errors
    /// `Unsent::Closed` when the receiver dropped, `Unsent::Cancelled` when a cancel arrived while
    /// the channel was full.
    pub(crate) fn send<T>(&self, tx: &Sender<T>, mut item: T) -> Result<(), Unsent> {
        let mut guard = lock(&self.gate);
        loop {
            match tx.try_send(item) {
                Ok(()) => return Ok(()),
                Err(TrySendError::Closed(_)) => return Err(Unsent::Closed),
                Err(TrySendError::Full(back)) => {
                    if self.is_cancelled() {
                        return Err(Unsent::Cancelled);
                    }
                    item = back;
                    guard = self
                        .wake
                        .wait(guard)
                        .unwrap_or_else(PoisonError::into_inner);
                }
            }
        }
    }
}

/// The SQLite side of cancellation: `sqlite3_interrupt` plus the flag the worker polls.
///
/// `is_alive` consumes a pending cancel (it clears the flag so the next operation starts clean), so it
/// must only be called after the operation's task has joined, as `rowx_core::cancel::settle` does.
#[derive(Clone)]
pub struct SqliteBackend {
    interrupt: Arc<InterruptHandle>,
    shared: Arc<Shared>,
}

impl fmt::Debug for SqliteBackend {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("SqliteBackend").finish_non_exhaustive()
    }
}

impl SqliteBackend {
    pub(crate) const fn new(interrupt: Arc<InterruptHandle>, shared: Arc<Shared>) -> Self {
        Self { interrupt, shared }
    }

    /// Resolves once a cancel has been requested, including one made before the wait began. The flag stays
    /// set until `is_alive` takes it, so this keeps resolving until then.
    pub async fn cancel_requested(&self) {
        self.shared.wait_cancelled().await;
    }

    /// Wakes the worker and keeps interrupting it until closing can proceed. SQLite ignores an
    /// interrupt sent between statements, including the gap just before the first step. While a
    /// [`Spared`] guard lives no interrupt is sent, so the statement it covers finishes; the cancel flag
    /// is still set, so a statement that has not started yet is refused.
    pub async fn interrupt_until_closed<F: Future>(&self, completion: F) -> F::Output {
        self.shared.request_cancel();
        tokio::pin!(completion);
        loop {
            {
                let spared = self
                    .shared
                    .spared
                    .lock()
                    .unwrap_or_else(PoisonError::into_inner);
                if !*spared {
                    self.interrupt.interrupt();
                }
            }
            if let Ok(result) =
                tokio::time::timeout(CLOSE_INTERRUPT_INTERVAL, &mut completion).await
            {
                return result;
            }
        }
    }

    /// Keeps closing from interrupting the statements run while the guard lives. Only for a statement
    /// whose outcome the caller must learn and that ends on its own, such as a `COMMIT`.
    #[must_use]
    pub fn spare(&self) -> Spared {
        *self
            .shared
            .spared
            .lock()
            .unwrap_or_else(PoisonError::into_inner) = true;
        Spared(Arc::clone(&self.shared))
    }

    #[cfg(feature = "test-support")]
    pub(crate) fn interrupt_handle(&self) -> Arc<InterruptHandle> {
        Arc::clone(&self.interrupt)
    }
}

impl Backend for SqliteBackend {
    type Error = Error;

    // The interrupt is connection-wide and does nothing while no statement runs; the flag covers a
    // cancel that lands before the next statement steps, and the settle loop resends the interrupt
    // until the operation's task ends.
    fn send_cancel(&self) -> impl Future<Output = Result<(), Error>> {
        self.shared.request_cancel();
        self.interrupt.interrupt();
        std::future::ready(Ok(()))
    }

    // Called after the task joined, which means the worker replied and is quiescent, so the flag is
    // taken here and the next operation starts clean. Calling it earlier would swallow a cancel that
    // is still pending.
    fn is_alive(&self) -> impl Future<Output = bool> {
        self.shared.clear_cancel();
        std::future::ready(self.shared.is_alive())
    }
}

/// Holds off closing's interrupts until it is dropped; see [`SqliteBackend::spare`].
#[derive(Debug)]
pub struct Spared(Arc<Shared>);

impl Drop for Spared {
    fn drop(&mut self) {
        *self.0.spared.lock().unwrap_or_else(PoisonError::into_inner) = false;
    }
}

pub type Cancel = rowx_core::cancel::Cancel<SqliteBackend>;
pub type Hook = rowx_core::cancel::Hook<SqliteBackend>;

#[cfg(test)]
mod tests {
    use std::sync::Arc;
    use std::thread;
    use std::time::Duration;

    use tokio::sync::mpsc::channel;
    use tokio::time::error::Elapsed;
    use tokio::time::timeout;

    use super::{Shared, Unsent};

    #[test]
    fn a_send_with_room_does_not_park() {
        let shared = Shared::default();
        let (tx, mut rx) = channel(2);
        assert_eq!(shared.send(&tx, 1), Ok(()));
        assert_eq!(shared.send(&tx, 2), Ok(()));
        assert_eq!(rx.try_recv().ok(), Some(1));
    }

    #[test]
    fn a_full_channel_parks_until_a_slot_frees() {
        let shared = Arc::new(Shared::default());
        let (tx, mut rx) = channel(1);
        assert_eq!(shared.send(&tx, 1), Ok(()));
        let worker = {
            let shared = Arc::clone(&shared);
            thread::spawn(move || shared.send(&tx, 2))
        };
        assert_eq!(rx.try_recv().ok(), Some(1));
        shared.wake();
        assert_eq!(worker.join().ok(), Some(Ok(())));
        assert_eq!(rx.try_recv().ok(), Some(2));
    }

    #[test]
    fn a_cancel_stops_a_worker_parked_on_a_full_channel() {
        let shared = Arc::new(Shared::default());
        let (tx, _rx) = channel(1);
        assert_eq!(shared.send(&tx, 1), Ok(()));
        let worker = {
            let shared = Arc::clone(&shared);
            thread::spawn(move || shared.send(&tx, 2))
        };
        shared.request_cancel();
        assert_eq!(worker.join().ok(), Some(Err(Unsent::Cancelled)));
    }

    #[test]
    fn a_dropped_receiver_stops_a_worker_parked_on_a_full_channel() {
        let shared = Arc::new(Shared::default());
        let (tx, mut rx) = channel(1);
        assert_eq!(shared.send(&tx, 1), Ok(()));
        let worker = {
            let shared = Arc::clone(&shared);
            thread::spawn(move || shared.send(&tx, 2))
        };
        rx.close();
        shared.wake();
        drop(rx);
        assert_eq!(worker.join().ok(), Some(Err(Unsent::Closed)));
    }

    #[test]
    fn a_cancel_flag_set_before_the_send_applies_only_when_full() {
        let shared = Shared::default();
        let (tx, _rx) = channel(1);
        shared.request_cancel();
        assert_eq!(shared.send(&tx, 1), Ok(()));
        assert_eq!(shared.send(&tx, 2), Err(Unsent::Cancelled));
        shared.clear_cancel();
        assert!(!shared.is_cancelled());
    }

    const GUARD: Duration = Duration::from_secs(5);
    const SHORT: Duration = Duration::from_millis(50);

    #[tokio::test]
    async fn a_waiter_spawned_before_the_request_resolves_on_it() -> Result<(), Elapsed> {
        let shared = Arc::new(Shared::default());
        let waiter = {
            let shared = Arc::clone(&shared);
            tokio::spawn(async move { shared.wait_cancelled().await })
        };
        tokio::task::yield_now().await;
        shared.request_cancel();
        let joined = timeout(GUARD, waiter).await?;
        assert!(joined.is_ok());
        Ok(())
    }

    #[tokio::test]
    async fn a_wait_started_after_the_request_returns_at_once() -> Result<(), Elapsed> {
        let shared = Shared::default();
        shared.request_cancel();
        timeout(GUARD, shared.wait_cancelled()).await
    }

    #[tokio::test]
    async fn a_wait_after_clear_cancel_resolves_only_on_the_next_request() -> Result<(), Elapsed> {
        let shared = Shared::default();
        shared.request_cancel();
        shared.clear_cancel();
        assert!(timeout(SHORT, shared.wait_cancelled()).await.is_err());
        shared.request_cancel();
        timeout(GUARD, shared.wait_cancelled()).await
    }
}

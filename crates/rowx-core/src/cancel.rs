use std::future::Future;
use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::{Arc, OnceLock};

use tokio::runtime::Handle;
use tokio::sync::oneshot;
use tokio::task::JoinHandle;

use crate::error::TaskStopped;
use crate::state::{Claim, State};

const RECANCEL_EVERY: std::time::Duration = std::time::Duration::from_millis(100);

/// The backend half of cancellation: how to ask the server to stop, and whether the connection survived.
pub trait Backend: Clone + Send + Sync + 'static {
    type Error: Send;

    /// Sends a cancel request and resolves once the server has acted on it.
    fn send_cancel(&self) -> impl Future<Output = Result<(), Self::Error>> + Send;

    /// `false` when the connection can no longer be used, so it must not return to service.
    fn is_alive(&self) -> impl Future<Output = bool> + Send;
}

const RUNNING: u8 = 0;
const DONE: u8 = 1;
const CANCELLED: u8 = 2;

// settles the race between an operation finishing and its caller being dropped: exactly one wins
#[derive(Debug, Default)]
struct Op(AtomicU8);

impl Op {
    fn finish(&self) -> bool {
        self.0
            .compare_exchange(RUNNING, DONE, Ordering::AcqRel, Ordering::Acquire)
            .is_ok()
    }

    fn cancel(&self) -> bool {
        self.0
            .compare_exchange(RUNNING, CANCELLED, Ordering::AcqRel, Ordering::Acquire)
            .is_ok()
    }
}

/// Everything a cancelled operation needs to settle its connection.
#[derive(Debug, Clone)]
pub struct Cancel<B> {
    pub state: Arc<State>,
    pub backend: B,
    pub handle: Handle,
}

/// Cancels an operation and hands the connection to a settling task; safe to call any number of times.
#[derive(Debug)]
struct Canceller<B> {
    op: Arc<Op>,
    task: std::sync::Mutex<Option<JoinHandle<()>>>,
    cancel: Cancel<B>,
}

impl<B: Backend> Canceller<B> {
    fn fire(&self) {
        if !self.op.cancel() {
            return;
        }
        let Some(task) = self.task.lock().ok().and_then(|mut slot| slot.take()) else {
            return;
        };
        // false means the connection was closed meanwhile, and close() cancels by itself
        if !self.cancel.state.settle() {
            return;
        }
        let cancel = self.cancel.clone();
        // this may run outside a runtime (a drop), so the stored handle spawns the settling task
        self.cancel.handle.spawn(settle(cancel, task));
    }
}

/// Lets a caller that learns of a cancellation synchronously (the Python future's done callback)
/// start the settling before the dropped future would. Create it before `spawn` and keep a clone.
#[derive(Debug)]
pub struct Hook<B>(Arc<OnceLock<Arc<Canceller<B>>>>);

impl<B> Clone for Hook<B> {
    fn clone(&self) -> Self {
        Self(Arc::clone(&self.0))
    }
}

impl<B> Default for Hook<B> {
    fn default() -> Self {
        Self(Arc::default())
    }
}

impl<B: Backend> Hook<B> {
    pub fn fire(&self) {
        if let Some(canceller) = self.0.get() {
            canceller.fire();
        }
    }
}

/// Dropping this while armed is a cancellation; `Pending::wait` disarms it on every completion path.
#[derive(Debug)]
struct Guard<B: Backend> {
    armed: bool,
    canceller: Arc<Canceller<B>>,
}

impl<B: Backend> Drop for Guard<B> {
    fn drop(&mut self) {
        if self.armed {
            self.canceller.fire();
        }
    }
}

async fn settle<B: Backend>(cancel: Cancel<B>, mut task: JoinHandle<()>) {
    // a CancelRequest only stops the statement running when it arrives, so one that lands between
    // the operation's round trips (while it encodes, or between prepare and execute) is ignored and
    // the next statement would run in full; resend until the operation ends. Only the abandoned
    // operation can use the backend meanwhile, since the state is Settling
    let drained = loop {
        // a finished operation has already consumed its response, so there is nothing left to cancel
        if task.is_finished() {
            break (&mut task).await.is_ok();
        }
        if cancel.backend.send_cancel().await.is_err() {
            cancel.state.settled(false);
            return;
        }
        // the abandoned operation drains to ReadyForQuery (or an error) before the next one starts
        if let Ok(joined) = tokio::time::timeout(RECANCEL_EVERY, &mut task).await {
            break joined.is_ok();
        }
    };
    let alive = cancel.backend.is_alive().await;
    cancel.state.settled(drained && alive);
}

/// A running operation; await `wait` for its result.
#[derive(Debug)]
pub struct Pending<T, B: Backend> {
    rx: oneshot::Receiver<T>,
    guard: Guard<B>,
}

impl<T, B: Backend> Pending<T, B> {
    /// # Errors
    /// `TaskStopped` when the operation task died before producing a result.
    pub async fn wait(mut self) -> Result<T, TaskStopped> {
        let result = (&mut self.rx).await;
        self.guard.armed = false;
        result.map_err(|_| TaskStopped)
    }
}

/// Runs `fut` as its own task, holding `claim` until it ends. Dropping the returned `Pending`
/// before `wait` completes cancels the operation and settles the connection.
pub fn spawn<B, T, F>(cancel: &Cancel<B>, claim: Claim, hook: &Hook<B>, fut: F) -> Pending<T, B>
where
    B: Backend,
    T: Send + 'static,
    F: Future<Output = T> + Send + 'static,
{
    let op = Arc::new(Op::default());
    let (tx, rx) = oneshot::channel();
    let task = {
        let op = Arc::clone(&op);
        cancel.handle.spawn(async move {
            let out = fut.await;
            if op.finish() {
                // Busy → Idle happens before the caller sees the result
                drop(claim);
            } else {
                // the canceller won and moves Busy → Settling itself
                claim.hand_off();
            }
            // the receiver is gone when the caller was dropped, which is the cancelled case
            let _ = tx.send(out);
        })
    };
    let canceller = Arc::new(Canceller {
        op,
        task: std::sync::Mutex::new(Some(task)),
        cancel: cancel.clone(),
    });
    // a hook that was already set belongs to an earlier operation, which cannot happen per call
    let _ = hook.0.set(Arc::clone(&canceller));
    Pending {
        rx,
        guard: Guard {
            armed: true,
            canceller,
        },
    }
}

/// The claim of a running stream, shared by its producer task and the consumer that may close it.
/// Whoever takes the claim out first releases it; later calls do nothing.
#[derive(Debug, Clone)]
pub struct StreamHandle {
    op: Arc<Op>,
    slot: Arc<std::sync::Mutex<Option<Claim>>>,
}

impl StreamHandle {
    /// Busy → Idle, or a hand-off to the canceller when the stream was cancelled meanwhile.
    pub fn release(&self) {
        let Some(claim) = self.slot.lock().ok().and_then(|mut slot| slot.take()) else {
            return;
        };
        if self.op.finish() {
            drop(claim);
        } else {
            claim.hand_off();
        }
    }
}

/// Runs the producer of a stream as its own task. Unlike `spawn`, the claim outlives any single call:
/// the producer, `StreamHandle::release` or the canceller (through `hook`) gives it up, whichever is first.
pub fn spawn_stream<B, F>(
    cancel: &Cancel<B>,
    claim: Claim,
    hook: &Hook<B>,
    make: impl FnOnce(StreamHandle) -> F,
) -> StreamHandle
where
    B: Backend,
    F: Future<Output = ()> + Send + 'static,
{
    let handle = StreamHandle {
        op: Arc::new(Op::default()),
        slot: Arc::new(std::sync::Mutex::new(Some(claim))),
    };
    let fut = make(handle.clone());
    let task = {
        let handle = handle.clone();
        cancel.handle.spawn(async move {
            fut.await;
            // a producer that ends without releasing (it was told to stop) still gives the claim up
            handle.release();
        })
    };
    let canceller = Arc::new(Canceller {
        op: Arc::clone(&handle.op),
        task: std::sync::Mutex::new(Some(task)),
        cancel: cancel.clone(),
    });
    let _ = hook.0.set(canceller);
    handle
}

#[cfg(test)]
mod stream_tests {
    use super::*;

    fn handle_with(state: &Arc<State>) -> Option<StreamHandle> {
        let claim = state.claim().ok().flatten()?;
        Some(StreamHandle {
            op: Arc::new(Op::default()),
            slot: Arc::new(std::sync::Mutex::new(Some(claim))),
        })
    }

    #[test]
    fn release_moves_busy_to_idle_once() {
        let state = Arc::new(State::default());
        let handle = handle_with(&state);
        assert!(handle.is_some());
        let Some(handle) = handle else { return };
        assert!(state.is_busy());
        handle.release();
        let next = state.claim();
        assert!(matches!(next, Ok(Some(_))));
        // the second release finds the slot empty and must not disturb the next operation
        handle.release();
        assert!(state.is_busy());
    }

    #[test]
    fn release_after_a_cancel_leaves_the_connection_for_the_canceller() {
        let state = Arc::new(State::default());
        let Some(handle) = handle_with(&state) else {
            return;
        };
        assert!(handle.op.cancel());
        assert!(state.settle());
        handle.release();
        assert_eq!(state.phase(), crate::state::Phase::Settling);
    }

    #[test]
    fn a_cancel_after_release_is_ignored() {
        let state = Arc::new(State::default());
        let Some(handle) = handle_with(&state) else {
            return;
        };
        handle.release();
        assert!(!handle.op.cancel());
        assert_eq!(state.phase(), crate::state::Phase::Idle);
    }
}

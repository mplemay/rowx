use std::future::Future;
use std::sync::{Arc, Mutex};

use tokio::runtime::Handle;
use tokio::sync::oneshot;
use tokio::task::JoinHandle;

use crate::error::{CoreError, TaskStopped};
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

/// Everything a cancelled operation needs to settle its connection.
#[derive(Debug, Clone)]
pub struct Cancel<B> {
    pub state: Arc<State>,
    pub backend: B,
    pub handle: Handle,
}

#[derive(Debug)]
enum Operation<B> {
    Waiting,
    Reserved(Claim),
    Running {
        claim: Claim,
        task: JoinHandle<()>,
        cancel: Cancel<B>,
    },
    Finished,
    Cancelled,
}

/// A reservation is visible to the Python done callback before the Rust future starts.
/// The same lock orders cancellation and completion with changes to the connection's claim.
#[derive(Debug)]
pub struct Hook<B>(Arc<Mutex<Operation<B>>>);

impl<B> Clone for Hook<B> {
    fn clone(&self) -> Self {
        Self(Arc::clone(&self.0))
    }
}

impl<B> Default for Hook<B> {
    fn default() -> Self {
        Self::new(None)
    }
}

impl<B> Hook<B> {
    #[must_use]
    pub fn new(claim: Option<Claim>) -> Self {
        Self(Arc::new(Mutex::new(
            claim.map_or(Operation::Waiting, Operation::Reserved),
        )))
    }

    /// Uses the synchronous reservation if present, or tries a deferred claim under the same lock.
    /// Returns false while a previous operation is settling.
    ///
    /// # Errors
    /// The claim error, or `Closed` if this operation was already cancelled or finished.
    pub fn reserve(
        &self,
        claim: impl FnOnce() -> Result<Option<Claim>, CoreError>,
    ) -> Result<bool, CoreError> {
        let mut operation = self.0.lock().map_err(|_| CoreError::Closed)?;
        match *operation {
            Operation::Reserved(_) => Ok(true),
            Operation::Waiting => {
                let Some(claim) = claim()? else {
                    return Ok(false);
                };
                *operation = Operation::Reserved(claim);
                drop(operation);
                Ok(true)
            }
            _ => Err(CoreError::Closed),
        }
    }

    fn start(
        &self,
        cancel: &Cancel<B>,
        future: impl Future<Output = ()> + Send + 'static,
    ) -> Result<(), TaskStopped>
    where
        B: Clone,
    {
        let mut operation = self.0.lock().map_err(|_| TaskStopped)?;
        let previous = std::mem::replace(&mut *operation, Operation::Finished);
        let Operation::Reserved(claim) = previous else {
            *operation = previous;
            return Err(TaskStopped);
        };
        *operation = Operation::Running {
            claim,
            task: cancel.handle.spawn(future),
            cancel: cancel.clone(),
        };
        drop(operation);
        Ok(())
    }

    /// Releases a reservation or a completed operation. A cancelled task settles separately.
    pub fn finish(&self) {
        if let Ok(mut operation) = self.0.lock()
            && !matches!(*operation, Operation::Cancelled | Operation::Finished)
        {
            // Releasing the claim and publishing Finished happen in the same critical section.
            *operation = Operation::Finished;
        }
    }
}

impl<B: Backend> Hook<B> {
    pub fn fire(&self) {
        let settling = {
            let Ok(mut operation) = self.0.lock() else {
                return;
            };
            match *operation {
                Operation::Finished | Operation::Cancelled => return,
                _ => {}
            }
            match std::mem::replace(&mut *operation, Operation::Cancelled) {
                Operation::Running {
                    claim,
                    task,
                    cancel,
                } => {
                    // A closed or broken connection must keep its terminal state.
                    if !cancel.state.settle() {
                        return;
                    }
                    claim.hand_off();
                    (cancel, task)
                }
                // A reservation without a task is released here without sending a server cancel.
                _ => return,
            }
        };
        let (cancel, task) = settling;
        let handle = cancel.handle.clone();
        handle.spawn(settle(cancel, task));
    }
}

struct FinishOnDrop<B>(Hook<B>);

impl<B> Drop for FinishOnDrop<B> {
    fn drop(&mut self) {
        self.0.finish();
    }
}

/// Dropping this while armed cancels the operation. Waiting disarms it on every completion path.
#[derive(Debug)]
struct Guard<B: Backend> {
    armed: bool,
    hook: Hook<B>,
}

impl<B: Backend> Drop for Guard<B> {
    fn drop(&mut self) {
        if self.armed {
            self.hook.fire();
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
        if result.is_err() {
            self.guard.hook.finish();
        }
        result.map_err(|_| TaskStopped)
    }
}

/// Runs the operation in its own task, holding the reservation until it completes or is cancelled.
///
/// # Errors
/// `TaskStopped` when the reservation is no longer available.
pub fn spawn<B, T, F>(
    cancel: &Cancel<B>,
    hook: &Hook<B>,
    fut: F,
) -> Result<Pending<T, B>, TaskStopped>
where
    B: Backend,
    T: Send + 'static,
    F: Future<Output = T> + Send + 'static,
{
    let (tx, rx) = oneshot::channel();
    let completion = hook.clone();
    hook.start(cancel, async move {
        let finish = FinishOnDrop(completion);
        let out = fut.await;
        drop(finish);
        let _ = tx.send(out);
    })?;
    Ok(Pending {
        rx,
        guard: Guard {
            armed: true,
            hook: hook.clone(),
        },
    })
}

/// Runs the stream producer in its own task. The claim can outlive a single read.
///
/// # Errors
/// `TaskStopped` when the reservation is no longer available.
pub fn spawn_stream<B: Backend>(
    cancel: &Cancel<B>,
    hook: &Hook<B>,
    fut: impl Future<Output = ()> + Send + 'static,
) -> Result<(), TaskStopped> {
    let completion = hook.clone();
    hook.start(cancel, async move {
        let _finish = FinishOnDrop(completion);
        fut.await;
    })
}

#[cfg(test)]
mod tests {
    use std::convert::Infallible;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::time::Duration;

    use tokio::sync::Notify;

    use super::*;
    use crate::state::Phase;

    #[derive(Clone, Default)]
    struct TestBackend {
        requests: Arc<AtomicUsize>,
        sent: Arc<Notify>,
        ack: Arc<Notify>,
    }

    impl Backend for TestBackend {
        type Error = Infallible;

        async fn send_cancel(&self) -> Result<(), Self::Error> {
            self.requests.fetch_add(1, Ordering::Relaxed);
            self.sent.notify_one();
            self.ack.notified().await;
            Ok(())
        }

        fn is_alive(&self) -> impl Future<Output = bool> {
            std::future::ready(true)
        }
    }

    fn cancel() -> Cancel<TestBackend> {
        Cancel {
            state: Arc::default(),
            backend: TestBackend::default(),
            handle: Handle::current(),
        }
    }

    #[tokio::test]
    async fn cancellation_before_start_releases_the_reservation() -> Result<(), CoreError> {
        let cancel = cancel();
        let hook = Hook::new(cancel.state.claim()?);
        assert_eq!(cancel.state.phase(), Phase::Busy);

        hook.fire();

        assert_eq!(cancel.state.phase(), Phase::Idle);
        assert!(spawn(&cancel, &hook, async {}).is_err());
        assert_eq!(cancel.backend.requests.load(Ordering::Relaxed), 0);
        Ok(())
    }

    #[tokio::test]
    async fn cancellation_before_deferred_claim_prevents_reservation() {
        let cancel = cancel();
        let hook = Hook::<TestBackend>::default();
        hook.fire();

        assert!(matches!(
            hook.reserve(|| cancel.state.claim()),
            Err(CoreError::Closed)
        ));
        assert_eq!(cancel.state.phase(), Phase::Idle);
    }

    #[tokio::test]
    async fn published_task_settles_once() -> Result<(), Box<dyn std::error::Error>> {
        let cancel = cancel();
        let hook = Hook::new(cancel.state.claim()?);
        let (release, wait) = oneshot::channel();
        let pending = spawn(&cancel, &hook, wait)?;

        hook.fire();
        assert_eq!(cancel.state.phase(), Phase::Settling);
        drop(pending);
        assert!(matches!(cancel.state.claim(), Ok(None)));

        tokio::time::timeout(Duration::from_secs(5), cancel.backend.sent.notified()).await?;
        assert!(release.send(()).is_ok());
        cancel.backend.ack.notify_one();
        tokio::time::timeout(Duration::from_secs(5), cancel.state.wait_settled()).await??;
        assert_eq!(cancel.state.phase(), Phase::Idle);
        assert_eq!(cancel.backend.requests.load(Ordering::Relaxed), 1);
        Ok(())
    }

    #[tokio::test]
    async fn cancellation_after_completion_leaves_the_next_claim_alone()
    -> Result<(), Box<dyn std::error::Error>> {
        let cancel = cancel();
        let hook = Hook::new(cancel.state.claim()?);
        spawn(&cancel, &hook, async {})?.wait().await?;

        let next = cancel.state.claim()?;
        assert!(next.is_some());
        hook.fire();
        assert_eq!(cancel.state.phase(), Phase::Busy);
        assert_eq!(cancel.backend.requests.load(Ordering::Relaxed), 0);
        drop(next);
        Ok(())
    }

    #[tokio::test]
    async fn releasing_a_stream_twice_leaves_the_next_claim_alone()
    -> Result<(), Box<dyn std::error::Error>> {
        let cancel = cancel();
        let hook = Hook::new(cancel.state.claim()?);
        let (release, wait) = oneshot::channel();
        spawn_stream(&cancel, &hook, async {
            let _ = wait.await;
        })?;
        hook.finish();

        let next = cancel.state.claim()?;
        assert!(next.is_some());
        hook.finish();
        hook.fire();
        assert_eq!(cancel.state.phase(), Phase::Busy);
        assert_eq!(cancel.backend.requests.load(Ordering::Relaxed), 0);
        assert!(release.send(()).is_ok());
        drop(next);
        Ok(())
    }
}

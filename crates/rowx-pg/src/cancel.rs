use std::future::Future;
use std::io;
use std::net::{IpAddr, SocketAddr};
use std::pin::Pin;
use std::sync::atomic::{AtomicU8, AtomicU64, Ordering};
use std::sync::{Arc, OnceLock};
use std::task::{Context, Poll};
use std::time::Duration;

use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, ReadBuf};
use tokio::net::TcpStream;
use tokio::runtime::Handle;
use tokio::sync::{Mutex, oneshot};
use tokio::task::JoinHandle;
use tokio_postgres::config::Host;
use tokio_postgres::tls::MakeTlsConnect;
use tokio_postgres::{CancelToken, Config};
use tokio_postgres_rustls::MakeRustlsConnect;

use crate::conn::Connection;
use crate::error::Error;
use crate::state::{Claim, State};

const CANCEL_TIMEOUT: Duration = Duration::from_secs(5);
const DEFAULT_PORT: u16 = 5432;

static REQUESTS_SENT: AtomicU64 = AtomicU64::new(0);

/// How many cancel requests this process has sent; tests use it to prove that errors send none.
#[must_use]
pub fn requests_sent() -> u64 {
    REQUESTS_SENT.load(Ordering::Acquire)
}

/// Where and how to send a `CancelRequest` for one connection.
#[derive(Clone)]
pub struct CancelTarget {
    token: CancelToken,
    host: Host,
    hostaddr: Option<IpAddr>,
    port: u16,
    tls: MakeRustlsConnect,
}

impl std::fmt::Debug for CancelTarget {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("CancelTarget")
            .field("host", &self.host)
            .field("port", &self.port)
            .finish_non_exhaustive()
    }
}

impl CancelTarget {
    /// # Errors
    /// `Interface` when the config names no host to reach.
    pub fn new(token: CancelToken, config: &Config, tls: MakeRustlsConnect) -> Result<Self, Error> {
        let host =
            config.get_hosts().first().cloned().ok_or_else(|| {
                Error::Interface("no host to send a cancel request to".to_owned())
            })?;
        Ok(Self {
            token,
            host,
            hostaddr: config.get_hostaddrs().first().copied(),
            port: config.get_ports().first().copied().unwrap_or(DEFAULT_PORT),
            tls,
        })
    }

    /// Opens its own socket, writes the `CancelRequest` and reads until the server closes it.
    /// The server closes only after it has signalled the backend, which tokio-postgres's
    /// `cancel_query` does not wait for.
    ///
    /// # Errors
    /// `Interface` for connection, IO or timeout failures.
    pub async fn request(&self) -> Result<(), Error> {
        REQUESTS_SENT.fetch_add(1, Ordering::AcqRel);
        tokio::time::timeout(CANCEL_TIMEOUT, self.send())
            .await
            .unwrap_or_else(|_| Err(Error::Interface("cancel request timed out".to_owned())))
    }

    async fn send(&self) -> Result<(), Error> {
        match &self.host {
            Host::Tcp(name) => {
                let stream = match self.hostaddr {
                    Some(ip) => TcpStream::connect(SocketAddr::new(ip, self.port)).await,
                    None => TcpStream::connect((name.as_str(), self.port)).await,
                }
                .map_err(|e| io_error(&e))?;
                self.over(stream, name).await
            }
            #[cfg(unix)]
            Host::Unix(dir) => {
                let path = dir.join(format!(".s.PGSQL.{}", self.port));
                let stream = tokio::net::UnixStream::connect(path)
                    .await
                    .map_err(|e| io_error(&e))?;
                // TLS is never negotiated over a unix socket, so the name is only a placeholder
                self.over(stream, "localhost").await
            }
        }
    }

    // the connector is the one the connection was made with, so the cancel is verified identically
    async fn over<S>(&self, stream: S, host: &str) -> Result<(), Error>
    where
        S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
    {
        // the TLS connector takes its stream by value, so a second handle on the raw socket is
        // kept to read until the server closes, which also works under TLS (ciphertext is discarded)
        let mut stream = Shared::new(stream);
        let owned = stream.clone();
        let tls = MakeTlsConnect::<Shared<S>>::make_tls_connect(&mut self.tls.clone(), host)
            .map_err(|e| Error::Interface(e.to_string()))?;
        self.token.cancel_query_raw(owned, tls).await?;
        let mut sink = [0_u8; 64];
        while stream.read(&mut sink).await.map_err(|e| io_error(&e))? != 0 {}
        Ok(())
    }
}

// two handles on one socket; the lock is only held inside a single poll
struct Shared<S>(Arc<std::sync::Mutex<S>>);

impl<S> Shared<S> {
    fn new(stream: S) -> Self {
        Self(Arc::new(std::sync::Mutex::new(stream)))
    }
}

impl<S> Clone for Shared<S> {
    fn clone(&self) -> Self {
        Self(Arc::clone(&self.0))
    }
}

impl<S: AsyncRead + AsyncWrite + Unpin> Shared<S> {
    fn with<R>(&self, f: impl FnOnce(Pin<&mut S>) -> Poll<io::Result<R>>) -> Poll<io::Result<R>> {
        self.0.lock().map_or_else(
            |_| Poll::Ready(Err(io::Error::other("cancel socket lock poisoned"))),
            |mut guard| f(Pin::new(&mut *guard)),
        )
    }
}

impl<S: AsyncRead + AsyncWrite + Unpin> AsyncRead for Shared<S> {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        self.with(|stream| stream.poll_read(cx, buf))
    }
}

impl<S: AsyncRead + AsyncWrite + Unpin> AsyncWrite for Shared<S> {
    fn poll_write(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<io::Result<usize>> {
        self.with(|stream| stream.poll_write(cx, buf))
    }

    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        self.with(|stream| stream.poll_flush(cx))
    }

    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        self.with(|stream| stream.poll_shutdown(cx))
    }
}

fn io_error(err: &std::io::Error) -> Error {
    Error::Interface(err.to_string())
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
pub struct Cancel {
    pub state: Arc<State>,
    pub target: CancelTarget,
    pub conn: Arc<Mutex<Connection>>,
    pub handle: Handle,
}

/// Cancels an operation and hands the connection to a settling task; safe to call any number of times.
#[derive(Debug)]
struct Canceller {
    op: Arc<Op>,
    task: std::sync::Mutex<Option<JoinHandle<()>>>,
    cancel: Cancel,
}

impl Canceller {
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
#[derive(Debug, Clone, Default)]
pub struct Hook(Arc<OnceLock<Arc<Canceller>>>);

impl Hook {
    pub fn fire(&self) {
        if let Some(canceller) = self.0.get() {
            canceller.fire();
        }
    }
}

/// Dropping this while armed is a cancellation; `Pending::wait` disarms it on every completion path.
#[derive(Debug)]
struct Guard {
    armed: bool,
    canceller: Arc<Canceller>,
}

impl Drop for Guard {
    fn drop(&mut self) {
        if self.armed {
            self.canceller.fire();
        }
    }
}

async fn settle(cancel: Cancel, task: JoinHandle<()>) {
    // a finished operation has already consumed its response, so there is nothing left to cancel
    if !task.is_finished() && cancel.target.request().await.is_err() {
        cancel.state.settled(false);
        return;
    }
    // the abandoned operation drains to ReadyForQuery (or an error) before the next one starts
    let drained = task.await.is_ok();
    let alive = cancel.conn.lock().await.client().is_ok();
    cancel.state.settled(drained && alive);
}

/// A running operation; await `wait` for its result.
#[derive(Debug)]
pub struct Pending<T> {
    rx: oneshot::Receiver<T>,
    guard: Guard,
}

impl<T> Pending<T> {
    /// # Errors
    /// `Interface` when the operation task died before producing a result.
    pub async fn wait(mut self) -> Result<T, Error> {
        let result = (&mut self.rx).await;
        self.guard.armed = false;
        result.map_err(|_| Error::Interface("the operation task stopped unexpectedly".to_owned()))
    }
}

/// Runs `fut` as its own task, holding `claim` until it ends. Dropping the returned `Pending`
/// before `wait` completes cancels the operation and settles the connection.
pub fn spawn<T, F>(cancel: &Cancel, claim: Claim, hook: &Hook, fut: F) -> Pending<T>
where
    T: Send + 'static,
    F: Future<Output = T> + Send + 'static,
{
    let op = Arc::new(Op::default());
    let (tx, rx) = oneshot::channel();
    let task = {
        let op = Arc::clone(&op);
        cancel.handle.spawn(async move {
            let out = fut.await;
            // after a cancellation the settling task owns the state, so the claim's drop is a no-op
            op.finish();
            // Busy → Idle happens before the caller sees the result
            drop(claim);
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

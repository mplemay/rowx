use std::io;
use std::net::{IpAddr, SocketAddr};
use std::pin::Pin;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::task::{Context, Poll};
use std::time::Duration;

use rowx_core::cancel::Backend;
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, ReadBuf};
use tokio::net::TcpStream;
use tokio::sync::Mutex;
use tokio_postgres::config::Host;
use tokio_postgres::tls::MakeTlsConnect;
use tokio_postgres::{CancelToken, Config};
use tokio_postgres_rustls::MakeRustlsConnect;

use crate::conn::Connection;
use crate::error::Error;

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
    // `None` when the config gives only a hostaddr
    host: Option<Host>,
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
    /// `Interface` when the config names no host or hostaddr to reach.
    pub fn new(token: CancelToken, config: &Config, tls: MakeRustlsConnect) -> Result<Self, Error> {
        let host = config.get_hosts().first().cloned();
        let hostaddr = config.get_hostaddrs().first().copied();
        if host.is_none() && hostaddr.is_none() {
            return Err(no_host());
        }
        Ok(Self {
            token,
            host,
            hostaddr,
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
        let Some(host) = &self.host else {
            let ip = self.hostaddr.ok_or_else(no_host)?;
            let stream = TcpStream::connect(SocketAddr::new(ip, self.port))
                .await
                .map_err(|e| io_error(&e))?;
            // tokio-postgres gives TLS an empty name when there is no host, so the cancel matches
            return self.over(stream, "").await;
        };
        match host {
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

fn no_host() -> Error {
    Error::Interface("no host to send a cancel request to".to_owned())
}

fn io_error(err: &std::io::Error) -> Error {
    Error::Interface(err.to_string())
}

/// The Postgres side of cancellation: a `CancelRequest` target plus the connection it belongs to.
#[derive(Debug, Clone)]
pub struct PgBackend {
    pub target: CancelTarget,
    pub conn: Arc<Mutex<Connection>>,
}

impl Backend for PgBackend {
    type Error = Error;

    async fn send_cancel(&self) -> Result<(), Error> {
        self.target.request().await
    }

    async fn is_alive(&self) -> bool {
        self.conn.lock().await.client().is_ok()
    }
}

pub type Cancel = rowx_core::cancel::Cancel<PgBackend>;
pub type Hook = rowx_core::cancel::Hook<PgBackend>;
pub use rowx_core::cancel::{StreamHandle, spawn, spawn_stream};

//! A scripted Postgres-protocol server on a local socket, for the downgrade tests.
//!
//! It runs on plain threads with blocking IO, so a test needs no extra async dependency, and it
//! records what each connection sent. Every connection gets one `Script`: whether to accept TLS and
//! what authentication to ask for. The last script repeats for any further connections.

use std::io::{self, Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::Duration;

use rustls::crypto::ring;
use rustls::{ServerConfig, ServerConnection, StreamOwned};

const SSL_REQUEST: u32 = 80_877_103;
const CANCEL_REQUEST: u32 = 80_877_102;
const PROTOCOL_3: u32 = 196_608;
const WAIT: Duration = Duration::from_secs(10);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Tls {
    /// Answers `N` to an `SSLRequest`.
    Refuse,
    /// Answers `S` and completes the handshake with the test certificate.
    Accept,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Auth {
    Ok,
    Cleartext,
    Md5,
    /// `AuthenticationSASL` offering only `SCRAM-SHA-256`.
    ScramWithoutPlus,
}

#[derive(Debug, Clone, Copy)]
pub struct Script {
    pub tls: Tls,
    pub auth: Auth,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum First {
    Startup,
    Cancel,
}

/// What one connection did, as the server saw it.
#[derive(Debug, Clone, Default)]
pub struct Seen {
    pub ssl_request: bool,
    /// The connection was encrypted before its first real message.
    pub tls: bool,
    /// The first message after the optional `SSLRequest`.
    pub first: Option<First>,
    /// Bytes the client sent in answer to a password or SASL request (zero if it closed instead).
    pub after_auth_request: usize,
}

impl Seen {
    #[must_use]
    pub fn startup_in_clear(&self) -> bool {
        self.first == Some(First::Startup) && !self.tls
    }

    #[must_use]
    pub fn startup_in_tls(&self) -> bool {
        self.first == Some(First::Startup) && self.tls
    }

    #[must_use]
    pub fn cancel_in_clear(&self) -> bool {
        self.first == Some(First::Cancel) && !self.tls
    }

    #[must_use]
    pub fn cancel_in_tls(&self) -> bool {
        self.first == Some(First::Cancel) && self.tls
    }
}

#[derive(Debug)]
struct Shared {
    done: Mutex<Vec<Seen>>,
    changed: Condvar,
}

#[derive(Debug)]
pub struct FakeServer {
    addr: SocketAddr,
    shared: Arc<Shared>,
}

impl FakeServer {
    /// # Errors
    /// When the socket cannot be bound or the TLS material cannot be read.
    pub fn start(scripts: Vec<Script>, certs: Option<&Path>) -> io::Result<Self> {
        let tls = certs.map(tls_config).transpose()?;
        let listener = TcpListener::bind("127.0.0.1:0")?;
        let addr = listener.local_addr()?;
        let shared = Arc::new(Shared {
            done: Mutex::new(Vec::new()),
            changed: Condvar::new(),
        });
        let background = Arc::clone(&shared);
        thread::spawn(move || {
            for (index, stream) in listener.incoming().enumerate() {
                let Ok(stream) = stream else { return };
                let Some(script) = scripts.get(index).or_else(|| scripts.last()).copied() else {
                    return;
                };
                let tls = tls.clone();
                let shared = Arc::clone(&background);
                thread::spawn(move || {
                    let seen = serve(stream, script, tls);
                    if let Ok(mut done) = shared.done.lock() {
                        done.push(seen);
                    }
                    shared.changed.notify_all();
                });
            }
        });
        Ok(Self { addr, shared })
    }

    #[must_use]
    pub const fn port(&self) -> u16 {
        self.addr.port()
    }

    /// Blocks until `count` connections have finished, then returns them in finishing order.
    ///
    /// # Errors
    /// When they have not finished within ten seconds.
    pub fn finished(&self, count: usize) -> io::Result<Vec<Seen>> {
        let timeout = || {
            io::Error::new(
                io::ErrorKind::TimedOut,
                "fake server connections unfinished",
            )
        };
        let mut done = self.shared.done.lock().map_err(|_| timeout())?;
        while done.len() < count {
            let (guard, result) = self
                .shared
                .changed
                .wait_timeout(done, WAIT)
                .map_err(|_| timeout())?;
            done = guard;
            if result.timed_out() && done.len() < count {
                return Err(timeout());
            }
        }
        Ok(done.clone())
    }
}

/// `$ROWX_TEST_CERTS`, else the repo's `.rowx-certs`, for tests that need a TLS server.
#[must_use]
pub fn certs_dir() -> PathBuf {
    std::env::var_os("ROWX_TEST_CERTS").map_or_else(
        || Path::new(env!("CARGO_MANIFEST_DIR")).join("../../.rowx-certs"),
        PathBuf::from,
    )
}

fn tls_config(dir: &Path) -> io::Result<Arc<ServerConfig>> {
    let invalid = |what: &str| io::Error::new(io::ErrorKind::InvalidData, what.to_owned());
    let chain = rustls_pemfile::certs(&mut io::BufReader::new(std::fs::File::open(
        dir.join("server.crt"),
    )?))
    .collect::<Result<Vec<_>, _>>()?;
    let key = rustls_pemfile::private_key(&mut io::BufReader::new(std::fs::File::open(
        dir.join("server.key"),
    )?))?
    .ok_or_else(|| invalid("no private key"))?;
    let config = ServerConfig::builder_with_provider(Arc::new(ring::default_provider()))
        .with_safe_default_protocol_versions()
        .map_err(|_| invalid("protocol versions"))?
        .with_no_client_auth()
        .with_single_cert(chain, key)
        .map_err(|_| invalid("certificate"))?;
    Ok(Arc::new(config))
}

enum Stream {
    Plain(TcpStream),
    Tls(Box<StreamOwned<ServerConnection, TcpStream>>),
}

impl Read for Stream {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        match self {
            Self::Plain(s) => s.read(buf),
            Self::Tls(s) => s.read(buf),
        }
    }
}

impl Write for Stream {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        match self {
            Self::Plain(s) => s.write(buf),
            Self::Tls(s) => s.write(buf),
        }
    }

    fn flush(&mut self) -> io::Result<()> {
        match self {
            Self::Plain(s) => s.flush(),
            Self::Tls(s) => s.flush(),
        }
    }
}

// the first message of a connection: no type byte, a length that counts itself, then the body
fn read_untyped(stream: &mut Stream) -> io::Result<Option<(u32, Vec<u8>)>> {
    let mut len = [0_u8; 4];
    if stream.read(&mut len[..1])? == 0 {
        return Ok(None);
    }
    stream.read_exact(&mut len[1..])?;
    let body_len = usize::try_from(u32::from_be_bytes(len))
        .map_err(|_| io::ErrorKind::InvalidData)?
        .checked_sub(4)
        .ok_or(io::ErrorKind::InvalidData)?;
    let mut body = vec![0_u8; body_len];
    stream.read_exact(&mut body)?;
    let code = body
        .get(..4)
        .and_then(|b| <[u8; 4]>::try_from(b).ok())
        .map(u32::from_be_bytes)
        .ok_or(io::ErrorKind::InvalidData)?;
    Ok(Some((code, body)))
}

fn message(kind: u8, body: &[u8]) -> io::Result<Vec<u8>> {
    let len = u32::try_from(body.len() + 4).map_err(|_| io::ErrorKind::InvalidInput)?;
    let mut out = vec![kind];
    out.extend_from_slice(&len.to_be_bytes());
    out.extend_from_slice(body);
    Ok(out)
}

fn auth_request(auth: Auth) -> io::Result<Vec<u8>> {
    match auth {
        Auth::Ok => message(b'R', &0_u32.to_be_bytes()),
        Auth::Cleartext => message(b'R', &3_u32.to_be_bytes()),
        Auth::Md5 => message(b'R', &[0, 0, 0, 5, 1, 2, 3, 4]),
        Auth::ScramWithoutPlus => {
            let mut body = 10_u32.to_be_bytes().to_vec();
            body.extend_from_slice(b"SCRAM-SHA-256\0\0");
            message(b'R', &body)
        }
    }
}

// what a real server sends after a successful authentication, enough for a session to open
fn session_start() -> io::Result<Vec<u8>> {
    let mut out = Vec::new();
    for (name, value) in [
        ("client_encoding", "UTF8"),
        ("standard_conforming_strings", "on"),
        ("server_version", "18.0"),
    ] {
        out.extend(message(b'S', format!("{name}\0{value}\0").as_bytes())?);
    }
    out.extend(message(b'K', &[0, 0, 0, 7, 0, 0, 0, 9])?);
    out.extend(message(b'Z', b"I")?);
    Ok(out)
}

// reads to the end of the stream; a client that gives up closes it, possibly without close_notify
fn drain(stream: &mut Stream) {
    let mut buf = [0_u8; 256];
    while stream.read(&mut buf).is_ok_and(|n| n > 0) {}
}

// what the connection did up to its first IO error, which a client closing early may cause
fn serve(tcp: TcpStream, script: Script, tls: Option<Arc<ServerConfig>>) -> Seen {
    let mut seen = Seen::default();
    let _ = converse(&mut seen, tcp, script, tls);
    seen
}

fn converse(
    seen: &mut Seen,
    tcp: TcpStream,
    script: Script,
    tls: Option<Arc<ServerConfig>>,
) -> io::Result<()> {
    tcp.set_read_timeout(Some(WAIT))?;
    let mut stream = Stream::Plain(tcp.try_clone()?);
    let mut first = read_untyped(&mut stream)?;
    if matches!(first, Some((SSL_REQUEST, _))) {
        seen.ssl_request = true;
        match (script.tls, tls) {
            (Tls::Accept, Some(config)) => {
                stream.write_all(b"S")?;
                let conn = ServerConnection::new(config).map_err(io::Error::other)?;
                stream = Stream::Tls(Box::new(StreamOwned::new(conn, tcp)));
                seen.tls = true;
            }
            _ => stream.write_all(b"N")?,
        }
        first = read_untyped(&mut stream)?;
    }
    match first {
        Some((CANCEL_REQUEST, _)) => {
            seen.first = Some(First::Cancel);
            // the client shuts its side down after the request; closing first would reset it
            drain(&mut stream);
            return Ok(());
        }
        Some((PROTOCOL_3, _)) => {
            seen.first = Some(First::Startup);
            stream.write_all(&auth_request(script.auth)?)?;
            if script.auth == Auth::Ok {
                stream.write_all(&session_start()?)?;
            }
            stream.flush()?;
            if script.auth == Auth::Ok {
                drain(&mut stream);
            } else {
                // a client that answers waits for the next message, so the first bytes are enough;
                // one that refuses closes the socket, which ends the read with nothing
                let mut buf = [0_u8; 256];
                seen.after_auth_request = stream.read(&mut buf).unwrap_or(0);
            }
            return Ok(());
        }
        _ => {}
    }
    drain(&mut stream);
    Ok(())
}

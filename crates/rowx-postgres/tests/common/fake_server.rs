//! A scripted Postgres-protocol server on a local socket, for the downgrade tests.
//!
//! It runs on plain threads with blocking IO, so a test needs no extra async dependency, and it
//! records what each connection sent. Every connection gets one `Script`: whether to accept TLS and
//! what authentication to ask for. The last script repeats for any further connections.

use std::io::{self, Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
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
    /// Answers `S`, then sends plaintext protocol messages instead of a handshake.
    AcceptThenPlaintext,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Auth {
    Ok,
    Cleartext,
    Md5,
    /// `AuthenticationSASL` offering only `SCRAM-SHA-256`.
    ScramWithoutPlus,
    /// `AuthenticationSASL` offering only `SCRAM-SHA-256-PLUS`.
    OnlyPlus,
    /// A SASL exchange whose server-final message carries a wrong signature, then `AuthenticationOk`.
    ScramBadFinal,
    /// `AuthenticationGSS`.
    Gss,
    /// `AuthenticationSSPI`.
    Sspi,
}

/// What the client did after the server's authentication request.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum Reply {
    #[default]
    /// The test never got that far.
    None,
    /// The client closed the connection without sending anything.
    Closed,
    /// The client sent this many bytes.
    Bytes(usize),
    /// Neither happened within the wait, which a test must treat as a failure.
    TimedOut,
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
    pub after_auth_request: Reply,
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
    stop: Arc<AtomicBool>,
    accept: Option<thread::JoinHandle<()>>,
}

impl FakeServer {
    /// # Errors
    /// When the socket cannot be bound or the TLS material cannot be read.
    pub fn start(scripts: Vec<Script>, certs: Option<&Path>) -> io::Result<Self> {
        Self::start_with_cert(scripts, certs, "server")
    }

    /// As `start`, serving the certificate `<stem>.crt` with `<stem>.key` from the directory.
    ///
    /// # Errors
    /// When the socket cannot be bound or the TLS material cannot be read.
    pub fn start_with_cert(
        scripts: Vec<Script>,
        certs: Option<&Path>,
        stem: &str,
    ) -> io::Result<Self> {
        let tls = certs.map(|dir| tls_config(dir, stem)).transpose()?;
        let listener = TcpListener::bind("127.0.0.1:0")?;
        let addr = listener.local_addr()?;
        let shared = Arc::new(Shared {
            done: Mutex::new(Vec::new()),
            changed: Condvar::new(),
        });
        let background = Arc::clone(&shared);
        let stop = Arc::new(AtomicBool::new(false));
        let stopping = Arc::clone(&stop);
        let accept = thread::spawn(move || {
            for (index, stream) in listener.incoming().enumerate() {
                if stopping.load(Ordering::SeqCst) {
                    return;
                }
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
        Ok(Self {
            addr,
            shared,
            stop,
            accept: Some(accept),
        })
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

impl Drop for FakeServer {
    fn drop(&mut self) {
        // the accept loop blocks in accept(); a last connection to ourselves wakes it to see the flag
        self.stop.store(true, Ordering::SeqCst);
        let _ = TcpStream::connect_timeout(&self.addr, WAIT);
        if let Some(accept) = self.accept.take() {
            let _ = accept.join();
        }
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

fn tls_config(dir: &Path, stem: &str) -> io::Result<Arc<ServerConfig>> {
    let invalid = |what: &str| io::Error::new(io::ErrorKind::InvalidData, what.to_owned());
    let chain = rustls_pemfile::certs(&mut io::BufReader::new(std::fs::File::open(
        dir.join(format!("{stem}.crt")),
    )?))
    .collect::<Result<Vec<_>, _>>()?;
    let key = rustls_pemfile::private_key(&mut io::BufReader::new(std::fs::File::open(
        dir.join(format!("{stem}.key")),
    )?))?
    .ok_or_else(|| invalid("no private key"))?;
    let mut config = ServerConfig::builder_with_provider(Arc::new(ring::default_provider()))
        .with_safe_default_protocol_versions()
        .map_err(|_| invalid("protocol versions"))?
        .with_no_client_auth()
        .with_single_cert(chain, key)
        .map_err(|_| invalid("certificate"))?;
    // tickets are extra records after the handshake; the script only wants postgres messages
    config.send_tls13_tickets = 0;
    Ok(Arc::new(config))
}

enum Stream {
    Plain(TcpStream),
    Tls(Box<StreamOwned<ServerConnection, TcpStream>>),
}

impl Read for Stream {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        match self {
            Self::Plain(stream) => stream.read(buf),
            Self::Tls(stream) => retry_interrupt(|| stream.read(buf)),
        }
    }
}

impl Write for Stream {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        match self {
            Self::Plain(stream) => stream.write(buf),
            Self::Tls(stream) => retry_interrupt(|| stream.write(buf)),
        }
    }

    fn flush(&mut self) -> io::Result<()> {
        match self {
            Self::Plain(stream) => stream.flush(),
            Self::Tls(stream) => retry_interrupt(|| stream.flush()),
        }
    }
}

// `EINTR` is not a result; rustls surfaces it and a blocking socket should try again.
// `WouldBlock` is left alone so a timeout stays a timeout instead of a spin.
fn retry_interrupt<T>(mut op: impl FnMut() -> io::Result<T>) -> io::Result<T> {
    loop {
        match op() {
            Err(error) if error.kind() == io::ErrorKind::Interrupted => {}
            result => return result,
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
        Auth::OnlyPlus => {
            let mut body = 10_u32.to_be_bytes().to_vec();
            body.extend_from_slice(b"SCRAM-SHA-256-PLUS\0\0");
            message(b'R', &body)
        }
        Auth::ScramBadFinal => {
            let mut body = 10_u32.to_be_bytes().to_vec();
            body.extend_from_slice(b"SCRAM-SHA-256-PLUS\0SCRAM-SHA-256\0\0");
            message(b'R', &body)
        }
        Auth::Gss => message(b'R', &7_u32.to_be_bytes()),
        Auth::Sspi => message(b'R', &9_u32.to_be_bytes()),
    }
}

// tokio-postgres drops the socket without `close_notify`, which rustls reports as `UnexpectedEof`
fn peer_gone(error: &io::Error) -> bool {
    matches!(
        error.kind(),
        io::ErrorKind::UnexpectedEof | io::ErrorKind::ConnectionReset | io::ErrorKind::BrokenPipe
    )
}

// `Ok(None)` means keep going; `Ok(Some(reply))` means the peer left and `reply` is the answer
fn proceed(result: io::Result<()>, reply: Reply) -> io::Result<Option<Reply>> {
    match result {
        Ok(()) => Ok(None),
        Err(error) if peer_gone(&error) => Ok(Some(reply)),
        Err(error) => Err(error),
    }
}

// one typed frontend message: a type byte, a length that counts itself, then the body
fn read_typed(stream: &mut Stream) -> io::Result<Option<Vec<u8>>> {
    let mut head = [0_u8; 5];
    if stream.read(&mut head[..1])? == 0 {
        return Ok(None);
    }
    stream.read_exact(&mut head[1..])?;
    let len = usize::try_from(u32::from_be_bytes([head[1], head[2], head[3], head[4]]))
        .map_err(|_| io::ErrorKind::InvalidData)?
        .checked_sub(4)
        .ok_or(io::ErrorKind::InvalidData)?;
    let mut body = vec![0_u8; len];
    stream.read_exact(&mut body)?;
    Ok(Some(body))
}

// a reset before a full message is the client going away, same as a clean end of stream
fn read_typed_or_reset(stream: &mut Stream) -> io::Result<Option<Vec<u8>>> {
    match read_typed(stream) {
        Err(error) if peer_gone(&error) => Ok(None),
        result => result,
    }
}

// the client-first message of a SASL initial response: mechanism, NUL, length, then the data
fn client_nonce(body: &[u8]) -> Option<String> {
    let nul = body.iter().position(|b| *b == 0)?;
    let data = String::from_utf8_lossy(body.get(nul.checked_add(5)?..)?).into_owned();
    data.split("r=").nth(1).map(str::to_owned)
}

// a SCRAM exchange that ends in a forged server-final message and then a success.
// once the client-first message is in hand, a later reset still reports those bytes.
fn bad_final(stream: &mut Stream) -> io::Result<Reply> {
    let Some(first) = read_typed_or_reset(stream)? else {
        return Ok(Reply::Closed);
    };
    let reply = Reply::Bytes(first.len() + 5);
    let nonce = client_nonce(&first).ok_or(io::ErrorKind::InvalidData)?;
    let mut challenge = 11_u32.to_be_bytes().to_vec();
    challenge.extend_from_slice(format!("r={nonce}srvnonce,s=c2FsdHNhbHQ=,i=4096").as_bytes());
    if let Some(done) = proceed(stream.write_all(&message(b'R', &challenge)?), reply)? {
        return Ok(done);
    }
    if let Some(done) = proceed(stream.flush(), reply)? {
        return Ok(done);
    }
    if read_typed_or_reset(stream)?.is_none() {
        return Ok(reply);
    }
    let mut last = 12_u32.to_be_bytes().to_vec();
    last.extend_from_slice(b"v=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=");
    if let Some(done) = proceed(stream.write_all(&message(b'R', &last)?), reply)? {
        return Ok(done);
    }
    if let Some(done) = proceed(
        stream.write_all(&message(b'R', &0_u32.to_be_bytes())?),
        reply,
    )? {
        return Ok(done);
    }
    if let Some(done) = proceed(stream.write_all(&session_start()?), reply)? {
        return Ok(done);
    }
    if let Some(done) = proceed(stream.flush(), reply)? {
        return Ok(done);
    }
    drain(stream);
    Ok(reply)
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

fn first_reply(stream: &mut Stream) -> Reply {
    let mut buf = [0_u8; 256];
    match stream.read(&mut buf) {
        Ok(0) => Reply::Closed,
        Ok(n) => Reply::Bytes(n),
        Err(e)
            if matches!(
                e.kind(),
                io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut
            ) =>
        {
            Reply::TimedOut
        }
        // a reset or a missing close_notify is the client going away
        Err(_) => Reply::Closed,
    }
}

// reads to the end of the stream; a client that gives up closes it, possibly without close_notify
fn drain(stream: &mut Stream) {
    let mut buf = [0_u8; 256];
    while stream.read(&mut buf).is_ok_and(|n| n > 0) {}
}

fn write_auth(stream: &mut Stream, auth: Auth) -> io::Result<()> {
    stream.write_all(&auth_request(auth)?)?;
    if auth == Auth::Ok {
        stream.write_all(&session_start()?)?;
    }
    stream.flush()
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
            (Tls::AcceptThenPlaintext, _) => {
                stream.write_all(b"S")?;
                stream.write_all(&auth_request(Auth::Ok)?)?;
                stream.write_all(&session_start()?)?;
                stream.flush()?;
                drain(&mut stream);
                return Ok(());
            }
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
            if let Err(error) = write_auth(&mut stream, script.auth) {
                // gone before any client bytes; record the close instead of leaving the default
                if script.auth == Auth::ScramBadFinal && peer_gone(&error) {
                    seen.after_auth_request = Reply::Closed;
                    return Ok(());
                }
                return Err(error);
            }
            if script.auth == Auth::ScramBadFinal {
                seen.after_auth_request = bad_final(&mut stream)?;
            } else {
                // a client that answers waits for the next message, so the first bytes are enough;
                // one that refuses closes the socket, which is an end of stream and not a timeout
                seen.after_auth_request = first_reply(&mut stream);
                if script.auth == Auth::Ok {
                    drain(&mut stream);
                }
            }
            return Ok(());
        }
        _ => {}
    }
    drain(&mut stream);
    Ok(())
}

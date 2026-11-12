// The front proxy: an accept thread plus one thread per connection, serving one session at a time over the engine's
// single backend. The engine's own endpoint cannot do this: a second client and every CancelRequest would wait in
// its listen backlog.
//
// The engine is one long-lived backend driven through `exec_protocol_raw_stream`, one call per batch of frontend
// messages. That shapes everything here: only whitelisted message types are forwarded (anything else can wedge the
// backend), `COPY ... FROM STDIN` is held back until its data is complete, session state is reset between sessions
// and verified, and a batch is never left unsent while the client waits for its answer.

use std::collections::{BTreeMap, HashMap};
use std::io::{self, Read, Write};
use std::net::Shutdown;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use rustix::event::{PollFd, PollFlags, Timespec, poll};
use rustix::net::{RecvFlags, recv};

use crate::classify::{FrameError, FrameReader, Startup, classify_startup, startup_parameters};
use crate::engine::{Engine, Sink};
use crate::error::{HostError, Result};
use crate::log;
use crate::slot::Slot;
use crate::sqlscan::{Effect, analyze};
use crate::wire::{
    ResponseScan, data_rows, error_parts, error_response, has_error, leading_strings,
    negotiate_protocol_version, parse_options, parse_text, query_text, quote_literal, simple_query,
    startup_response, sync,
};

const READ_BYTES: usize = 64 * 1024;
// how long a connection may take to finish its startup packet and handshake
const STARTUP_TIMEOUT: Duration = Duration::from_secs(10);
// how long a startup waits for the previous session's ROLLBACK / DISCARD ALL; generous because a loaded machine
// stretches the teardown, and it only delays a client that arrives during one (an Active session refuses at once)
const TEARDOWN_WAIT: Duration = Duration::from_secs(10);
// how long a startup waits for a statement whose client has already gone (it cannot be interrupted)
const ABANDONED_WAIT: Duration = Duration::from_secs(30);
const ACCEPT_POLL: Duration = Duration::from_millis(200);
const ACCEPT_BACKOFF: Duration = Duration::from_millis(50);
// the whole data of a COPY FROM STDIN is held in memory until its CopyDone
const MAX_COPY_BYTES: usize = 1024 * 1024 * 1024;
const COPY_SAVEPOINT: &str = "rowx_copy_probe";
const BUSY_HINT: &str = "an embedded database serves one session at a time; use max_size=1";
const REPORTED: &str = "'server_version','server_encoding','client_encoding','DateStyle','TimeZone','integer_datetimes',\
'standard_conforming_strings','IntervalStyle','application_name','default_transaction_read_only',\
'in_hot_standby','is_superuser','scram_iterations','session_authorization'";
pub const SOCKET_NAME: &str = ".s.PGSQL.5432";

#[derive(Debug)]
struct Shared {
    engine: Arc<Engine>,
    slot: Slot,
    shutdown: AtomicBool,
    key: i32,
    next_id: AtomicU64,
    // live client sockets, so closing the instance can end their sessions
    connections: Mutex<HashMap<u64, UnixStream>>,
    // the client of the session that holds the slot, to tell a live second client from an abandoned statement
    active_peer: Mutex<Option<UnixStream>>,
    pid: u32,
}

#[derive(Debug)]
pub struct Front {
    path: PathBuf,
    shared: Arc<Shared>,
    accept: Option<JoinHandle<()>>,
}

fn accept_loop(listener: &UnixListener, shared: &Arc<Shared>) {
    let timeout = Timespec {
        tv_sec: 0,
        tv_nsec: i64::try_from(ACCEPT_POLL.as_nanos()).unwrap_or(200_000_000),
    };
    while !shared.shutdown.load(Ordering::SeqCst) {
        // wakes at once for a connection, and at least every ACCEPT_POLL to look at the shutdown flag, so closing never
        // depends on the socket file still existing
        let mut fds = [PollFd::new(listener, PollFlags::IN)];
        let _ = poll(&mut fds, Some(&timeout));
        match listener.accept() {
            Ok((stream, _)) => {
                // BSD-derived systems hand the accepted socket the listener's non-blocking flag
                if stream.set_nonblocking(false).is_err() {
                    continue;
                }
                let shared = Arc::clone(shared);
                let _ = thread::Builder::new()
                    .name("rowx-conn".into())
                    .spawn(move || {
                        let id = shared.next_id.fetch_add(1, Ordering::SeqCst);
                        if let (Ok(clone), Ok(mut live)) =
                            (stream.try_clone(), shared.connections.lock())
                        {
                            live.insert(id, clone);
                        }
                        if let Err(error) = handle(&shared, stream) {
                            log(&format!("connection ended: {error}"));
                        }
                        if let Ok(mut live) = shared.connections.lock() {
                            live.remove(&id);
                        }
                    });
            }
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {}
            // out of file descriptors and the like: do not spin
            Err(_) => thread::sleep(ACCEPT_BACKOFF),
        }
    }
}

impl Front {
    /// Listens on `<dir>/.s.PGSQL.5432`.
    ///
    /// # Errors
    /// When the socket cannot be bound or the accept thread cannot start.
    pub fn start(dir: &Path, engine: Arc<Engine>) -> Result<Self> {
        let path = dir.join(SOCKET_NAME);
        let listener = UnixListener::bind(&path)?;
        listener.set_nonblocking(true)?;
        let key = i32::try_from(std::process::id() ^ 0x5eed_u32).unwrap_or(7);
        let shared = Arc::new(Shared {
            engine,
            slot: Slot::default(),
            shutdown: AtomicBool::new(false),
            key,
            next_id: AtomicU64::new(0),
            connections: Mutex::new(HashMap::new()),
            active_peer: Mutex::new(None),
            pid: std::process::id(),
        });
        let accept = {
            let shared = Arc::clone(&shared);
            thread::Builder::new()
                .name("rowx-accept".into())
                .spawn(move || accept_loop(&listener, &shared))?
        };
        Ok(Self {
            path,
            shared,
            accept: Some(accept),
        })
    }

    /// Stops accepting, ends every client session and removes the socket. A second call, or a call from a forked
    /// child, does nothing to the parent's sockets or threads.
    pub fn close(&mut self) {
        if std::process::id() != self.shared.pid {
            if let Some(accept) = self.accept.take() {
                std::mem::forget(accept);
            }
            return;
        }
        self.shared.shutdown.store(true, Ordering::SeqCst);
        if let Some(accept) = self.accept.take() {
            let _ = accept.join();
        }
        if let Ok(mut live) = self.shared.connections.lock() {
            for stream in live.values() {
                let _ = stream.shutdown(Shutdown::Both);
            }
            live.clear();
        }
        let _ = std::fs::remove_file(&self.path);
    }
}

impl Drop for Front {
    fn drop(&mut self) {
        self.close();
    }
}

fn invalid(error: impl std::fmt::Display) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, error.to_string())
}

// whether the client at the other end of `stream` has hung up. A hang-up is reported even when unread data is still
// queued (a client that sent a query and closed), which a plain read cannot see.
fn peer_gone(stream: &UnixStream) -> bool {
    let now = Timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    let mut fds = [PollFd::new(stream, PollFlags::IN)];
    if poll(&mut fds, Some(&now)).is_ok() && fds[0].revents().contains(PollFlags::HUP) {
        return true;
    }
    let mut byte = [0_u8; 1];
    match recv(stream, &mut byte[..], RecvFlags::PEEK | RecvFlags::DONTWAIT) {
        Ok((_, read)) => read == 0,
        Err(error) => error != rustix::io::Errno::AGAIN,
    }
}

fn handle(shared: &Arc<Shared>, mut stream: UnixStream) -> io::Result<()> {
    // a client that connects and says nothing must not hold a thread forever
    stream.set_read_timeout(Some(STARTUP_TIMEOUT))?;
    let mut reader = FrameReader::default();
    let mut buf = vec![0_u8; READ_BYTES];
    loop {
        while let Some(packet) = reader.next_startup().map_err(invalid)? {
            match classify_startup(&packet) {
                Ok(Startup::Ssl | Startup::Gss) => stream.write_all(b"N")?,
                // read and closed at once: the client's cancel guard sees EOF and drains the running statement
                Ok(Startup::Cancel) => return Ok(()),
                Ok(Startup::V3) => return session(shared, stream, reader, &packet, false),
                Ok(Startup::V3Newer(_)) => return session(shared, stream, reader, &packet, true),
                Err(FrameError::UnsupportedCode(code)) => {
                    let text = format!(
                        "unsupported frontend protocol {}.{}",
                        code >> 16,
                        code & 0xffff
                    );
                    stream.write_all(&error_response("FATAL", "0A000", &text, None))?;
                    return Ok(());
                }
                Err(error) => return Err(invalid(error)),
            }
        }
        let n = stream.read(&mut buf)?;
        if n == 0 {
            return Ok(());
        }
        reader.push(&buf[..n]);
    }
}

fn session(
    shared: &Arc<Shared>,
    mut stream: UnixStream,
    reader: FrameReader,
    startup: &[u8],
    newer: bool,
) -> io::Result<()> {
    let client_gone = || {
        shared
            .active_peer
            .lock()
            .ok()
            .and_then(|peer| peer.as_ref().map(peer_gone))
            .unwrap_or(false)
    };
    if !shared
        .slot
        .acquire(TEARDOWN_WAIT, ABANDONED_WAIT, &client_gone)
    {
        stream.write_all(&error_response(
            "FATAL",
            "53300",
            "sorry, too many clients already",
            Some(BUSY_HINT),
        ))?;
        return Ok(());
    }
    if let (Ok(clone), Ok(mut peer)) = (stream.try_clone(), shared.active_peer.lock()) {
        *peer = Some(clone);
    }
    let result = Session::new(shared, &stream).run(reader, startup, newer);
    // the slot leaves Active before the peer is forgotten: a startup that sees Active with no peer would refuse
    shared.slot.begin_teardown();
    if let Ok(mut peer) = shared.active_peer.lock() {
        *peer = None;
    }
    reset(&shared.engine);
    let _ = stream.shutdown(Shutdown::Both);
    shared.slot.finish_teardown();
    result
}

// What a real server does when a connection ends: nothing of the session survives into the next one. A leading Sync
// leaves the "ignore until Sync" state an abandoned extended-protocol error would otherwise keep the backend in, and
// the outcome is verified: a backend that cannot be put back to idle is unusable, not silently handed on.
fn reset(engine: &Engine) {
    if !engine.is_running() || engine.unusable_reason().is_some() {
        return;
    }
    let request = [
        sync(),
        simple_query("ROLLBACK"),
        simple_query("DISCARD ALL"),
    ]
    .concat();
    match engine.raw(request) {
        Ok(response) => {
            let mut scan = ResponseScan::default();
            scan.feed(&response);
            if scan.errors > 0 || scan.ready_count != 3 || scan.ready_status != Some(b'I') {
                engine
                    .mark_unusable("resetting the session after its client left did not complete");
            }
        }
        Err(HostError::Engine(reason)) => engine.mark_unusable(&reason),
        Err(error) => log(&format!("reset: {error}")),
    }
}

// `SELECT set_config(...), ...` for the settings, or None when there are none
fn settings_sql(settings: &[(String, String)]) -> Option<String> {
    if settings.is_empty() {
        return None;
    }
    let calls: Vec<String> = settings
        .iter()
        .map(|(name, value)| {
            format!(
                "set_config({}, {}, false)",
                quote_literal(name),
                quote_literal(value)
            )
        })
        .collect();
    Some(format!("SELECT {}", calls.join(", ")))
}

fn apply(engine: &Engine, settings: &[(String, String)]) -> Result<Vec<u8>> {
    settings_sql(settings).map_or_else(|| Ok(Vec::new()), |sql| engine.raw(simple_query(&sql)))
}

fn fatal_from(response: &[u8], fallback: &str) -> Vec<u8> {
    let (code, text) =
        error_parts(response).unwrap_or_else(|| ("XX000".to_owned(), fallback.to_owned()));
    error_response("FATAL", &code, &text, None)
}

// the settings to re-apply after a client reset, and the startup response
type Started = (Vec<(String, String)>, Vec<u8>);

// applies the connection's startup settings and builds the startup response; Err carries a ready ErrorResponse
fn begin(
    shared: &Shared,
    engine: &Engine,
    startup: &[u8],
    newer: bool,
) -> std::result::Result<Started, Vec<u8>> {
    let engine_err = |error: HostError| error_response("FATAL", "XX000", &error.to_string(), None);
    let params: BTreeMap<&str, &str> = startup_parameters(startup)
        .map_err(|e| error_response("FATAL", "08P01", &e.to_string(), None))?;
    if let Some(reason) = engine.unusable_reason() {
        return Err(engine_err(HostError::Unusable(reason)));
    }
    if !engine.is_running() {
        return Err(error_response(
            "FATAL",
            "57P01",
            "the embedded engine is not running",
            None,
        ));
    }
    let user = params.get("user").copied().unwrap_or("postgres");
    if let Some(database) = params.get("database").filter(|d| **d != "postgres") {
        return Err(error_response(
            "FATAL",
            "3D000",
            &format!("database \"{database}\" does not exist"),
            None,
        ));
    }
    let mut settings: Vec<(String, String)> = Vec::new();
    // `_pq_.` options are protocol extensions this server does not know: they are reported, never set
    let mut unrecognized: Vec<&str> = Vec::new();
    for (name, value) in &params {
        if name.starts_with("_pq_.") {
            unrecognized.push(name);
            continue;
        }
        if matches!(*name, "user" | "database" | "replication" | "options") {
            continue;
        }
        settings.push(((*name).to_owned(), (*value).to_owned()));
    }
    if let Some(options) = params.get("options") {
        settings.extend(parse_options(options));
    }
    // set unconditionally: a plain RESET does not undo an earlier session's authorization in a single backend
    settings.push(("session_authorization".to_owned(), user.to_owned()));
    let response = apply(engine, &settings).map_err(engine_err)?;
    if has_error(&response) {
        return Err(fatal_from(&response, "invalid startup parameter"));
    }
    let query = format!("SELECT name, setting FROM pg_settings WHERE name IN ({REPORTED})");
    let response = engine.raw(simple_query(&query)).map_err(engine_err)?;
    let reported: Vec<(String, String)> = data_rows(&response)
        .into_iter()
        .filter_map(|row| match <[Option<String>; 2]>::try_from(row) {
            Ok([Some(name), Some(value)]) => Some((name, value)),
            _ => None,
        })
        .collect();
    let mut out = if newer || !unrecognized.is_empty() {
        // a real server answers a newer minor version (or unknown protocol options) by downgrading to 3.0
        negotiate_protocol_version(&unrecognized)
    } else {
        Vec::new()
    };
    out.extend(startup_response(&reported, shared.key));
    Ok((settings, out))
}

// the data of a COPY ... FROM STDIN, collected until the client says it is done
#[derive(Debug)]
struct CopyIn {
    statement: Vec<u8>,
    data: Vec<Vec<u8>>,
    bytes: usize,
}

#[derive(Debug)]
enum Mode {
    Normal,
    // an extended-protocol statement was refused: everything up to the next Sync is discarded
    SkipUntilSync,
    Copy(CopyIn),
}

enum Flow {
    Continue,
    End,
}

// the output of a streamed submission
#[derive(Debug, Default)]
struct Delivery {
    scan: ResponseScan,
    client_gone: bool,
}

struct Session<'a> {
    shared: &'a Shared,
    engine: &'a Arc<Engine>,
    stream: &'a UnixStream,
    settings: Vec<(String, String)>,
    // transaction status of the last ReadyForQuery the engine sent: I, T or E
    status: u8,
    mode: Mode,
    // frames received but not yet sent to the engine, and how many of them (a prefix) may be sent
    batch: Vec<Vec<u8>>,
    sendable: usize,
    // Setting resets in a prepared statement or portal take effect when it is executed, which can be in a later
    // round trip than the Parse: they are followed by name.
    statements: HashMap<Vec<u8>, Vec<Effect>>,
    portals: HashMap<Vec<u8>, Vec<Effect>>,
}

fn message_frame(tag: u8, body: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(body.len() + 5);
    out.push(tag);
    out.extend_from_slice(
        &i32::try_from(body.len() + 4)
            .unwrap_or(i32::MAX)
            .to_be_bytes(),
    );
    out.extend_from_slice(body);
    out
}

const RESTORE_STATEMENT: &[u8] = b"rowx_restore\0";
// ParseComplete, BindComplete, DataRow, CommandComplete and two CloseComplete
const RESTORE_ANSWER_MESSAGES: usize = 6;

// the extended-protocol messages that run `sql` on a private statement and portal and close them again
fn restore_frames(sql: &str) -> Vec<Vec<u8>> {
    let mut parse = RESTORE_STATEMENT.to_vec();
    parse.extend_from_slice(sql.as_bytes());
    parse.extend_from_slice(&[0, 0, 0]);
    let mut bind = RESTORE_STATEMENT.to_vec();
    bind.extend_from_slice(RESTORE_STATEMENT);
    bind.extend_from_slice(&[0; 6]);
    let mut execute = RESTORE_STATEMENT.to_vec();
    execute.extend_from_slice(&0_i32.to_be_bytes());
    let mut close_statement = vec![b'S'];
    close_statement.extend_from_slice(RESTORE_STATEMENT);
    let mut close_portal = vec![b'P'];
    close_portal.extend_from_slice(RESTORE_STATEMENT);
    vec![
        message_frame(b'P', &parse),
        message_frame(b'B', &bind),
        message_frame(b'E', &execute),
        message_frame(b'C', &close_statement),
        message_frame(b'C', &close_portal),
    ]
}

/// `response` without its last `count` messages.
#[must_use]
pub fn strip_trailing_messages(response: &[u8], count: usize) -> Vec<u8> {
    let mut ends = Vec::new();
    let mut cursor = 0;
    while let Some(header) = response.get(cursor..cursor + 5) {
        let Ok(length_bytes) = <[u8; 4]>::try_from(&header[1..5]) else {
            break;
        };
        let end = usize::try_from(i32::from_be_bytes(length_bytes))
            .map_or(usize::MAX, |length| cursor + 1 + length);
        if end > response.len() || end < cursor + 5 {
            break;
        }
        ends.push(end);
        cursor = end;
    }
    let keep = ends.len().saturating_sub(count);
    let cut = if keep == 0 { 0 } else { ends[keep - 1] };
    response[..cut].to_vec()
}

/// The bytes of `response` without its messages tagged `tag`.
#[must_use]
pub fn strip_messages(response: &[u8], tag: u8) -> Vec<u8> {
    let mut out = Vec::with_capacity(response.len());
    let mut cursor = 0;
    while let Some(header) = response.get(cursor..cursor + 5) {
        let Ok(length_bytes) = <[u8; 4]>::try_from(&header[1..5]) else {
            break;
        };
        let end = usize::try_from(i32::from_be_bytes(length_bytes))
            .map_or(usize::MAX, |length| cursor + 1 + length);
        if end > response.len() || end < cursor + 5 {
            break;
        }
        if header[0] != tag {
            out.extend_from_slice(&response[cursor..end]);
        }
        cursor = end;
    }
    out.extend_from_slice(&response[cursor..]);
    out
}

impl<'a> Session<'a> {
    fn new(shared: &'a Shared, stream: &'a UnixStream) -> Self {
        Self {
            shared,
            engine: &shared.engine,
            stream,
            settings: Vec::new(),
            status: b'I',
            mode: Mode::Normal,
            batch: Vec::new(),
            sendable: 0,
            statements: HashMap::new(),
            portals: HashMap::new(),
        }
    }

    fn write(&self, bytes: &[u8]) -> io::Result<()> {
        let mut out = self.stream;
        out.write_all(bytes)
    }

    // an error the client is told about before the session ends
    fn fatal(&self, code: &str, text: &str) -> io::Error {
        let _ = self.write(&error_response("FATAL", code, text, None));
        io::Error::other(text.to_owned())
    }

    // the engine failed at the protocol level (not an SQL error): its session state is unknown from here on
    fn engine_failed(&self, error: &HostError) -> io::Error {
        if !matches!(
            error,
            HostError::Unusable(_) | HostError::EngineGone | HostError::ForkedProcess
        ) {
            self.engine.mark_unusable(&error.to_string());
        }
        let text = self.engine.unusable_reason().map_or_else(
            || error.to_string(),
            |reason| HostError::Unusable(reason).to_string(),
        );
        let code = if matches!(error, HostError::EngineGone) {
            "57P01"
        } else {
            "XX000"
        };
        self.fatal(code, &text)
    }

    // sends frames to the engine, streaming its answer to the client. Ok(false) means the client went away.
    fn submit(&mut self, frames: &[Vec<u8>]) -> io::Result<bool> {
        if frames.is_empty() {
            return Ok(true);
        }
        let request = frames.concat();
        let delivery = Arc::new(Mutex::new(Delivery::default()));
        let mut out = self.stream.try_clone()?;
        let state = Arc::clone(&delivery);
        let sink: Sink = Box::new(move |chunk| {
            if let Ok(mut delivery) = state.lock() {
                delivery.scan.feed(chunk);
            }
            out.write_all(chunk).inspect_err(|_| {
                if let Ok(mut delivery) = state.lock() {
                    delivery.client_gone = true;
                }
            })
        });
        let result = self.engine.stream(request, sink);
        let (status, gone) = delivery.lock().map_or((None, false), |delivery| {
            (delivery.scan.ready_status, delivery.client_gone)
        });
        if let Some(status) = status {
            self.status = status;
        }
        match result {
            Ok(()) => Ok(true),
            Err(_) if gone => {
                log("the client went away mid-response");
                Ok(false)
            }
            Err(error) => Err(self.engine_failed(&error)),
        }
    }

    // sends frames to the engine and returns its whole answer instead of streaming it
    fn capture(&mut self, frames: &[Vec<u8>]) -> io::Result<(Vec<u8>, ResponseScan)> {
        let response = self
            .engine
            .raw(frames.concat())
            .map_err(|error| self.engine_failed(&error))?;
        let mut scan = ResponseScan::default();
        scan.feed(&response);
        if let Some(status) = scan.ready_status {
            self.status = status;
        }
        Ok((response, scan))
    }

    fn submit_through(&mut self, count: usize) -> io::Result<bool> {
        let count = count.min(self.batch.len());
        let head: Vec<Vec<u8>> = self.batch.drain(..count).collect();
        self.sendable = self.sendable.saturating_sub(count);
        self.submit(&head)
    }

    fn submit_all(&mut self) -> io::Result<bool> {
        self.submit_through(self.batch.len())
    }

    // the startup settings a reset of `effects` must bring back
    fn settings_for(&self, effects: &[Effect]) -> Vec<(String, String)> {
        let everything = effects.contains(&Effect::All);
        self.settings
            .iter()
            .filter(|(name, _)| {
                everything || effects.contains(&Effect::Setting(name.to_ascii_lowercase()))
            })
            .cloned()
            .collect()
    }

    // a real server restores the startup options when a client resets a setting; a single backend does not
    fn reapply(&self, effects: &[Effect]) {
        if let Err(error) = apply(self.engine, &self.settings_for(effects)) {
            log(&format!("re-applying startup settings: {error}"));
        }
    }

    fn run(&mut self, mut reader: FrameReader, startup: &[u8], newer: bool) -> io::Result<()> {
        let (settings, response) = match begin(self.shared, self.engine, startup, newer) {
            Ok(started) => started,
            Err(error) => {
                self.write(&error)?;
                return Ok(());
            }
        };
        self.settings = settings;
        self.write(&response)?;
        self.stream.set_read_timeout(None)?;
        let mut buf = vec![0_u8; READ_BYTES];
        loop {
            for frame in reader
                .drain_messages()
                .map_err(|error| self.fatal("08P01", &error.to_string()))?
            {
                if matches!(self.handle_frame(frame)?, Flow::End) {
                    return Ok(());
                }
            }
            // whatever is complete up to the last flush point goes now: the client may be waiting for it
            if self.sendable > 0 && !self.submit_through(self.sendable)? {
                return Ok(());
            }
            let n = (&*self.stream).read(&mut buf)?;
            if n == 0 {
                // the client left without Terminate; frames it never flushed are dropped
                return Ok(());
            }
            reader.push(&buf[..n]);
        }
    }

    fn handle_frame(&mut self, frame: Vec<u8>) -> io::Result<Flow> {
        let tag = frame.first().copied().unwrap_or(0);
        match &mut self.mode {
            Mode::Copy(copy) => match tag {
                b'd' => {
                    copy.bytes += frame.len();
                    if copy.bytes > MAX_COPY_BYTES {
                        return Err(
                            self.fatal("53200", "COPY data is too large for an embedded database")
                        );
                    }
                    copy.data.push(frame);
                    Ok(Flow::Continue)
                }
                b'c' | b'f' => self.finish_copy(frame),
                // allowed and ignored while a copy is in progress
                b'H' | b'S' => Ok(Flow::Continue),
                // the client gave up: nothing was ever executed
                b'X' => Ok(Flow::End),
                other => Err(self.fatal(
                    "08P01",
                    &format!("unexpected message type {} during COPY", char::from(other)),
                )),
            },
            Mode::SkipUntilSync => match tag {
                b'S' => {
                    self.mode = Mode::Normal;
                    Ok(if self.submit(&[frame])? {
                        Flow::Continue
                    } else {
                        Flow::End
                    })
                }
                b'X' => Ok(Flow::End),
                _ => Ok(Flow::Continue),
            },
            Mode::Normal => self.handle_normal(tag, frame),
        }
    }

    fn handle_normal(&mut self, tag: u8, frame: Vec<u8>) -> io::Result<Flow> {
        match tag {
            b'X' => {
                self.submit_all()?;
                Ok(Flow::End)
            }
            b'Q' => self.handle_query(frame),
            b'P' => self.handle_parse(frame),
            b'B' => {
                self.track_bind(&frame);
                self.batch.push(frame);
                Ok(Flow::Continue)
            }
            b'E' => self.handle_execute(frame),
            b'D' | b'C' => {
                self.batch.push(frame);
                Ok(Flow::Continue)
            }
            b'H' => {
                self.batch.push(frame);
                self.sendable = self.batch.len();
                Ok(Flow::Continue)
            }
            b'S' => Ok(self.handle_sync(frame)),
            // copy messages outside a copy are accepted and ignored, as a real server does
            b'd' | b'c' | b'f' => Ok(Flow::Continue),
            b'F' => self.refuse_and_resync(
                "the function call protocol is not supported by the embedded engine",
            ),
            other => {
                self.submit_all()?;
                Err(self.fatal(
                    "08P01",
                    &format!("invalid frontend message type {:?}", char::from(other)),
                ))
            }
        }
    }

    // the client asked for something unsupported outside the extended protocol: it gets the error and a ReadyForQuery
    fn refuse_and_resync(&mut self, text: &str) -> io::Result<Flow> {
        if !self.submit_all()? {
            return Ok(Flow::End);
        }
        self.write(&error_response("ERROR", "0A000", text, None))?;
        Ok(if self.submit(&[sync()])? {
            Flow::Continue
        } else {
            Flow::End
        })
    }

    fn handle_query(&mut self, frame: Vec<u8>) -> io::Result<Flow> {
        let analysis = query_text(&frame)
            .map(|text| analyze(&text))
            .unwrap_or_default();
        if analysis.copy_from_stdin {
            if !self.submit_all()? {
                return Ok(Flow::End);
            }
            return self.start_copy(frame);
        }
        self.batch.push(frame);
        self.sendable = self.batch.len();
        if !analysis.effects.is_empty() {
            if !self.submit_through(self.sendable)? {
                return Ok(Flow::End);
            }
            self.reapply(&analysis.effects);
        }
        Ok(Flow::Continue)
    }

    fn handle_parse(&mut self, frame: Vec<u8>) -> io::Result<Flow> {
        let analysis = parse_text(&frame)
            .map(|text| analyze(&text))
            .unwrap_or_default();
        if analysis.copy_from_stdin {
            // the data must be held back, which is only possible when the whole statement arrives as one simple
            // Query message
            if !self.submit_all()? {
                return Ok(Flow::End);
            }
            self.write(&error_response(
                "ERROR",
                "0A000",
                "COPY FROM STDIN through the extended query protocol is not supported by the embedded engine; use \
                 the simple query protocol",
                None,
            ))?;
            self.mode = Mode::SkipUntilSync;
            return Ok(Flow::Continue);
        }
        if let Some((name, _)) = leading_strings(&frame) {
            if analysis.effects.is_empty() {
                self.statements.remove(name);
            } else {
                self.statements.insert(name.to_vec(), analysis.effects);
            }
        }
        self.batch.push(frame);
        Ok(Flow::Continue)
    }

    fn track_bind(&mut self, frame: &[u8]) {
        let Some((portal, statement)) = leading_strings(frame) else {
            return;
        };
        match self.statements.get(statement) {
            Some(effects) => {
                self.portals.insert(portal.to_vec(), effects.clone());
            }
            None => {
                self.portals.remove(portal);
            }
        }
    }

    // A statement that resets settings is sent to the engine right after its Execute, and the startup settings are
    // restored before anything pipelined behind it runs. The engine is mid-cycle (no Sync yet), where a simple Query
    // would break it, so the restore goes in as extended-protocol messages of its own on a private statement, and
    // its answer is cut off the end of the response.
    fn handle_execute(&mut self, frame: Vec<u8>) -> io::Result<Flow> {
        let effects = leading_strings(&frame)
            .and_then(|(portal, _)| self.portals.get(portal))
            .cloned()
            .unwrap_or_default();
        self.batch.push(frame);
        let Some(sql) = settings_sql(&self.settings_for(&effects)).filter(|_| !effects.is_empty())
        else {
            return Ok(Flow::Continue);
        };
        let mut frames: Vec<Vec<u8>> = std::mem::take(&mut self.batch);
        self.sendable = 0;
        frames.extend(restore_frames(&sql));
        let (response, scan) = self.capture(&frames)?;
        // after an error the engine skipped the restore, and there is nothing of ours to remove
        let answer = if scan.errors == 0 {
            strip_trailing_messages(&response, RESTORE_ANSWER_MESSAGES)
        } else {
            response
        };
        self.write(&answer)?;
        Ok(Flow::Continue)
    }

    fn handle_sync(&mut self, frame: Vec<u8>) -> Flow {
        self.batch.push(frame);
        self.sendable = self.batch.len();
        Flow::Continue
    }

    // The engine returns from a call as soon as it has answered CopyInResponse, and the next call that carries the
    // data finds its protocol state wedged. So the data is held here and the whole copy goes in one call. The client
    // will not send data before it has seen CopyInResponse, so the statement is first run with an immediate CopyFail
    // to obtain that message (inside a savepoint when a transaction is open, so the client's transaction survives).
    fn start_copy(&mut self, statement: Vec<u8>) -> io::Result<Flow> {
        let in_transaction = self.status == b'T';
        if in_transaction {
            let savepoint = simple_query(&format!("SAVEPOINT {COPY_SAVEPOINT}"));
            let (_, scan) = self.capture(&[savepoint])?;
            if scan.errors > 0 {
                return Err(self.fatal("XX000", "could not start COPY: savepoint failed"));
            }
        }
        let probe = [statement.clone(), message_frame(b'f', b"rowx probe\0")].concat();
        let (response, scan) = self.capture(&[probe])?;
        let Some(copy_in) = scan.copy_in else {
            // the statement failed before the copy started: the client gets exactly that error
            self.write(&response)?;
            return Ok(Flow::Continue);
        };
        if in_transaction {
            let cleanup = simple_query(&format!(
                "ROLLBACK TO SAVEPOINT {COPY_SAVEPOINT}; RELEASE SAVEPOINT {COPY_SAVEPOINT}"
            ));
            let (_, scan) = self.capture(&[cleanup])?;
            if scan.errors > 0 || scan.ready_status != Some(b'T') {
                self.engine
                    .mark_unusable("could not undo the COPY probe inside a transaction");
                return Err(self.fatal("XX000", "could not start COPY: savepoint cleanup failed"));
            }
        }
        self.write(&message_frame(b'G', &copy_in))?;
        self.mode = Mode::Copy(CopyIn {
            statement,
            data: Vec::new(),
            bytes: 0,
        });
        Ok(Flow::Continue)
    }

    fn finish_copy(&mut self, end: Vec<u8>) -> io::Result<Flow> {
        let Mode::Copy(copy) = std::mem::replace(&mut self.mode, Mode::Normal) else {
            return Ok(Flow::Continue);
        };
        let mut frames = Vec::with_capacity(copy.data.len() + 2);
        frames.push(copy.statement);
        frames.extend(copy.data);
        frames.push(end);
        let (response, _) = self.capture(&frames)?;
        // the client has already seen CopyInResponse
        self.write(&strip_messages(&response, b'G'))?;
        Ok(Flow::Continue)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn trailing_messages_are_cut_off_the_end() {
        let mut response = message_frame(b'1', &[]);
        response.extend(message_frame(b'D', b"row"));
        response.extend(message_frame(b'C', b"tag"));
        let mut expected = message_frame(b'1', &[]);
        expected.extend(message_frame(b'D', b"row"));
        assert_eq!(strip_trailing_messages(&response, 1), expected);
        assert_eq!(strip_trailing_messages(&response, 3), Vec::<u8>::new());
        assert_eq!(strip_trailing_messages(&response, 9), Vec::<u8>::new());
        assert_eq!(strip_trailing_messages(&response, 0), response);
    }

    #[test]
    fn a_message_tag_is_stripped_from_a_response() {
        let mut response = message_frame(b'G', &[0, 0, 0]);
        response.extend(message_frame(b'C', b"COPY 2\0"));
        response.extend(message_frame(b'Z', b"I"));
        let stripped = strip_messages(&response, b'G');
        let mut expected = message_frame(b'C', b"COPY 2\0");
        expected.extend(message_frame(b'Z', b"I"));
        assert_eq!(stripped, expected);
        assert_eq!(strip_messages(&expected, b'G'), expected);
    }
}

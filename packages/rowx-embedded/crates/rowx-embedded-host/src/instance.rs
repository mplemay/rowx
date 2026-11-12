// An embedded database: a private root directory holding the public socket, the engine thread and the front proxy.

use std::fmt::Write as _;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use oliphaunt_wasix::{Oliphaunt, StorageErrorCode};

use crate::engine::{Engine, EngineOptions, Storage};
use crate::error::{HostError, Result};
use crate::owner;
use crate::proxy::{Front, SOCKET_NAME};
use crate::runtime;
use crate::seed;
use crate::snapshot::{self, Header, Identity};

// sun_path is 104 bytes on macOS and 108 on Linux, including the terminating NUL
const MAX_SOCKET_PATH: usize = 100;
const RESTORE_DIR: &str = "db";
const SEED_WORK_DIR: &str = "seed-work";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Boot {
    // no snapshot: initdb in memory, or an existing / new database directory
    Fresh,
    // a memory database booted from a derived cluster seed (the fast path)
    Seed,
    // a snapshot restored into a directory (the documented path; also the fallback of `Seed`)
    Restore,
}

impl Boot {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Fresh => "fresh",
            Self::Seed => "seed",
            Self::Restore => "restore",
        }
    }
}

// background closes still running, so exit can wait for them instead of leaving their directories behind
static REAPERS: Mutex<Vec<(u32, std::thread::JoinHandle<()>)>> = Mutex::new(Vec::new());

/// Waits for every close started by [`Embedded::close_in_background`] in this process.
pub fn join_background_closes() {
    let handles = REAPERS
        .lock()
        .map(|mut reapers| std::mem::take(&mut *reapers))
        .unwrap_or_default();
    for (pid, handle) in handles {
        if pid == std::process::id() {
            let _ = handle.join();
        } else {
            // a thread of the parent: it does not exist in a forked child and must not be joined
            std::mem::forget(handle);
        }
    }
}

#[derive(Debug)]
pub struct Embedded {
    root: PathBuf,
    directory: Option<PathBuf>,
    dsn: String,
    boot: Boot,
    engine: Arc<Engine>,
    // held for the whole of `close`, so a second closer waits for the first to finish
    front: Mutex<Option<Front>>,
    closed: AtomicBool,
    // set by `abandon`: a later `close` (an atexit sweep) must not wait for a closer that is still stuck on the engine
    abandoned: AtomicBool,
    pid: u32,
    // keeps the private root marked as live for sweepers in other processes
    _lock: owner::RootLock,
}

// the identity every snapshot records and every restore checks
///
/// # Errors
/// When the engine's asset manifest is unreadable.
pub fn identity() -> Result<Identity> {
    Ok(Identity {
        engine_version: runtime::ENGINE_VERSION.to_owned(),
        runtime_version: runtime::runtime_version().to_owned(),
        wasm_sha256: runtime::wasm_sha256()?.to_owned(),
        extensions: runtime::EXTENSIONS
            .iter()
            .map(|name| (*name).to_owned())
            .collect(),
    })
}

fn percent_encode_path(path: &Path) -> String {
    let mut out = String::new();
    for byte in path.to_string_lossy().bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'~' | b'/' => {
                out.push(char::from(byte));
            }
            other => {
                let _ = write!(out, "%{other:02X}");
            }
        }
    }
    out
}

#[must_use]
pub fn dsn_for(socket_dir: &Path) -> String {
    format!(
        "postgresql://postgres@/postgres?host={}&sslmode=disable",
        percent_encode_path(socket_dir)
    )
}

// removes a private root unless the start succeeds
struct RootGuard(Option<PathBuf>);

impl RootGuard {
    fn disarm(&mut self) -> PathBuf {
        self.0.take().unwrap_or_default()
    }
}

impl Drop for RootGuard {
    fn drop(&mut self) {
        if let Some(root) = self.0.take() {
            let _ = fs::remove_dir_all(root);
        }
    }
}

// the private root, created 0700 and locked for the life of the instance
fn make_root() -> Result<(PathBuf, owner::RootLock)> {
    let base = std::env::temp_dir();
    // a start is the natural moment to clean up after processes that died without closing
    owner::sweep_orphans(&base, &owner::process_alive);
    let pid = std::process::id();
    // owner-only from the moment it exists: the socket in it is the only access control (trust authentication)
    let root = tempfile::Builder::new()
        .prefix(&owner::root_prefix(pid))
        .permissions(fs::Permissions::from_mode(0o700))
        .tempdir_in(&base)?
        .keep();
    // a relative TMPDIR must not leak into the DSN: the client may run in another working directory
    let root = fs::canonicalize(&root).unwrap_or(root);
    let socket = root.join(SOCKET_NAME);
    if socket.as_os_str().len() > MAX_SOCKET_PATH {
        let _ = fs::remove_dir_all(&root);
        return Err(HostError::InvalidArgument(format!(
            "the socket path {} is too long for a Unix socket; set TMPDIR to a shorter directory",
            socket.display()
        )));
    }
    match owner::lock_root(&root) {
        Ok(lock) => Ok((root, lock)),
        Err(error) => {
            let _ = fs::remove_dir_all(&root);
            Err(HostError::Io(error))
        }
    }
}

fn restore(directory: &Path, payload: &[u8]) -> Result<()> {
    if let Some(parent) = directory.parent() {
        fs::create_dir_all(parent)?;
    }
    Oliphaunt::restore(directory, payload).map_err(|error| {
        // restoring into a directory another instance holds must say who, like opening it does
        if error
            .storage_error()
            .is_some_and(|details| details.code() == StorageErrorCode::Busy)
        {
            HostError::Locked {
                directory: directory.to_path_buf(),
                pid: owner::live_owner(directory),
            }
        } else {
            HostError::from(error)
        }
    })
}

// the cached or newly derived seed; ROWX_EMBEDDED_TEST_FAIL_SEED makes it fail, to exercise the directory fallback
fn seed_for(header: &Header, payload: &[u8], root: &Path) -> Result<Arc<seed::Seed>> {
    if std::env::var_os("ROWX_EMBEDDED_TEST_FAIL_SEED").is_some() {
        return Err(HostError::Engine(
            "seed derivation disabled for a test".into(),
        ));
    }
    seed::cached(header).map_or_else(
        || seed::derive_cached(header, payload, &root.join(SEED_WORK_DIR)),
        Ok,
    )
}

fn boot_engine(
    root: &Path,
    directory: Option<&Path>,
    snapshot: Option<(&Header, &[u8])>,
) -> Result<(Engine, Boot)> {
    match (directory, snapshot) {
        (None, None) => {
            let options = EngineOptions {
                storage: Storage::Memory,
                seed: None,
                fast: true,
            };
            Ok((Engine::start(options)?, Boot::Fresh))
        }
        (Some(directory), None) => {
            let options = EngineOptions {
                storage: Storage::Directory(directory.to_path_buf()),
                seed: None,
                fast: false,
            };
            Ok((Engine::start(options)?, Boot::Fresh))
        }
        (Some(directory), Some((_, payload))) => {
            restore(directory, payload)?;
            let options = EngineOptions {
                storage: Storage::Directory(directory.to_path_buf()),
                seed: None,
                fast: false,
            };
            Ok((Engine::start(options)?, Boot::Restore))
        }
        (None, Some((header, payload))) => {
            let attempt = seed_for(header, payload, root).and_then(|seed| {
                Engine::start(EngineOptions {
                    storage: Storage::Memory,
                    seed: Some(seed),
                    fast: true,
                })
            });
            match attempt {
                Ok(engine) => Ok((engine, Boot::Seed)),
                Err(error) => {
                    crate::log(&format!(
                        "seed boot failed, restoring into a directory instead: {error}"
                    ));
                    let directory = root.join(RESTORE_DIR);
                    restore(&directory, payload)?;
                    let options = EngineOptions {
                        storage: Storage::Directory(directory),
                        seed: None,
                        fast: true,
                    };
                    Ok((Engine::start(options)?, Boot::Restore))
                }
            }
        }
    }
}

impl Embedded {
    /// Starts an instance behind a front proxy and returns once the socket accepts connections.
    ///
    /// `directory` makes the database persistent (one owner per directory); `snapshot` seeds a new database from
    /// [`Embedded::snapshot`] bytes. With both, the snapshot is restored into the (absent or empty) directory.
    ///
    /// # Errors
    /// When the arguments are invalid, the snapshot does not match this build, the directory is in use, or the
    /// engine or the socket cannot start.
    pub fn start(directory: Option<&Path>, snapshot: Option<&[u8]>) -> Result<Self> {
        if let Some(directory) = directory.filter(|d| !d.is_absolute()) {
            return Err(HostError::InvalidArgument(format!(
                "the database directory must be an absolute path, not {}",
                directory.display()
            )));
        }
        let parsed = snapshot
            .map(|bytes| -> Result<(Header, &[u8])> {
                let (header, payload) = snapshot::decode(bytes)?;
                header.check(&identity()?)?;
                Ok((header, payload))
            })
            .transpose()?;
        // the whole payload is hashed before anything trusts it, including a cached seed derived from the header
        if let Some((header, payload)) = &parsed {
            header.verify_payload(payload)?;
        }
        let (root, lock) = make_root()?;
        let mut guard = RootGuard(Some(root.clone()));
        let (engine, boot) = boot_engine(&root, directory, parsed.as_ref().map(|(h, p)| (h, *p)))?;
        let engine = Arc::new(engine);
        let front = Front::start(&root, Arc::clone(&engine))?;
        if let Some(directory) = directory {
            // the engine holds its lock now; record who we are for a second opener's error message
            owner::write_owner(directory, std::process::id());
        }
        let root = guard.disarm();
        Ok(Self {
            dsn: dsn_for(&root),
            root,
            directory: directory.map(Path::to_path_buf),
            boot,
            engine,
            front: Mutex::new(Some(front)),
            closed: AtomicBool::new(false),
            abandoned: AtomicBool::new(false),
            pid: std::process::id(),
            _lock: lock,
        })
    }

    #[must_use]
    pub fn dsn(&self) -> &str {
        &self.dsn
    }

    #[must_use]
    pub fn directory(&self) -> Option<&Path> {
        self.directory.as_deref()
    }

    #[must_use]
    pub const fn boot(&self) -> Boot {
        self.boot
    }

    #[must_use]
    pub fn root(&self) -> &Path {
        &self.root
    }

    /// A self-describing physical snapshot of the live instance, taken without stopping it.
    ///
    /// # Errors
    /// When the engine fails or this is a forked child.
    pub fn snapshot(&self) -> Result<Vec<u8>> {
        if std::process::id() != self.pid {
            return Err(HostError::ForkedProcess);
        }
        if self.is_closed() || !self.engine.is_running() {
            return Err(HostError::Closed);
        }
        let backup = self.engine.backup()?;
        Ok(snapshot::encode(&identity()?, &backup)?)
    }

    /// Stops the proxy, shuts the engine down cleanly and removes the socket directory and any private temp root.
    /// Idempotent, and a no-op in any process other than the one that started the instance.
    ///
    /// # Errors
    /// When the engine fails to shut down (the files are removed regardless).
    pub fn close(&self) -> Result<()> {
        if std::process::id() != self.pid {
            return Ok(());
        }
        if self.abandoned.load(Ordering::SeqCst) {
            return Ok(());
        }
        // the guard is held until the instance is fully closed: a concurrent closer blocks here and then finds nothing
        let mut slot = self.front.lock().map_err(|_| HostError::EngineGone)?;
        self.closed.store(true, Ordering::SeqCst);
        let Some(mut front) = slot.take() else {
            return Ok(());
        };
        front.close();
        let result = self.engine.close();
        if let Some(directory) = &self.directory {
            owner::remove_owner(directory);
        }
        let _ = fs::remove_dir_all(&self.root);
        drop(slot);
        result
    }

    /// Stops accepting clients, removes the socket directory and any private temp root, and marks the instance closed,
    /// all without waiting for the engine thread: a statement that is still running is left to finish or to die with
    /// the process. For a user that is leaving and wants nothing of a memory database.
    ///
    /// A persistent directory is refused unless `force` is set, because abandoning skips the clean engine shutdown;
    /// the directory is then left as a crash would leave it (it runs with fsync on, so it opens again). A no-op in any
    /// process other than the one that started the instance, and idempotent with [`Embedded::close`].
    ///
    /// # Errors
    /// When the instance is persistent and `force` is not set.
    pub fn abandon(&self, force: bool) -> Result<()> {
        if std::process::id() != self.pid {
            return Ok(());
        }
        if self.directory.is_some() && !force {
            return Err(HostError::InvalidArgument(
                "a persistent database cannot be abandoned without force; close it".into(),
            ));
        }
        self.closed.store(true, Ordering::SeqCst);
        self.abandoned.store(true, Ordering::SeqCst);
        // a close that is waiting on the engine holds this lock: it removes the files itself when it finishes, so the
        // abandon only takes the root away now
        if let Ok(mut slot) = self.front.try_lock()
            && let Some(mut front) = slot.take()
        {
            front.close();
        }
        // the engine's own locks are never held while it waits, so this cannot block behind a closer
        self.engine.abandon();
        let _ = fs::remove_dir_all(&self.root);
        Ok(())
    }

    /// Whether `close` has started (or finished).
    #[must_use]
    pub fn is_closed(&self) -> bool {
        self.closed.load(Ordering::SeqCst)
    }

    /// Closes on a background thread so the caller (a destructor holding the GIL) never waits on the engine.
    /// In a forked child the instance is leaked instead: its threads and files belong to the parent.
    pub fn close_in_background(self: Arc<Self>) {
        if std::process::id() != self.pid {
            std::mem::forget(self);
            return;
        }
        let pid = self.pid;
        let reaper = std::thread::Builder::new()
            .name("rowx-reaper".into())
            .spawn(move || {
                let _ = self.close();
            });
        if let (Ok(reaper), Ok(mut reapers)) = (reaper, REAPERS.lock()) {
            reapers.retain(|(_, handle)| !handle.is_finished());
            reapers.push((pid, reaper));
        }
    }
}

impl Drop for Embedded {
    fn drop(&mut self) {
        if std::process::id() == self.pid {
            let _ = self.close();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_dsn_is_a_plain_socket_dsn() {
        assert_eq!(
            dsn_for(Path::new("/tmp/rxe-1-abc")),
            "postgresql://postgres@/postgres?host=/tmp/rxe-1-abc&sslmode=disable"
        );
    }

    #[test]
    fn unusual_path_bytes_are_percent_encoded() {
        let dsn = dsn_for(Path::new("/tmp/with space&more"));
        assert_eq!(
            dsn,
            "postgresql://postgres@/postgres?host=/tmp/with%20space%26more&sslmode=disable"
        );
    }

    #[test]
    fn a_relative_directory_is_refused_before_anything_starts() {
        let error = Embedded::start(Some(Path::new("relative/db")), None)
            .err()
            .map(|e| e.to_string());
        assert!(error.is_some_and(|text| text.contains("absolute path")));
    }

    #[test]
    fn a_foreign_snapshot_is_refused_before_anything_starts() {
        let error = Embedded::start(None, Some(b"not a snapshot"))
            .err()
            .map(|e| e.to_string());
        assert!(error.is_some_and(|text| text.contains("not a rowx-embedded snapshot")));
    }
}

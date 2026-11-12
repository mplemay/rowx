// One dedicated OS thread owns the non-Send engine handle and serves commands over a channel. Every blocking call
// from another thread waits on a reply channel, so the engine itself never runs on a Python or proxy thread.

use std::io;
use std::path::PathBuf;
use std::sync::mpsc::{Sender, channel};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};

use oliphaunt_wasix::{ClusterSeed, DatabaseStorage, Oliphaunt, StorageErrorCode};

use crate::error::{HostError, Result};
use crate::owner;
use crate::runtime;
use crate::seed::Seed;
use crate::wire::simple_query;

const ENGINE_STACK: usize = 8 * 1024 * 1024;

// engine threads that `abandon` let go of, with the process that owns them. The handles are kept (never dropped) so
// the process can tell whether one is still inside a statement: libc teardown under a running Wasmer thread crashes
static ABANDONED: Mutex<Vec<(u32, JoinHandle<()>)>> = Mutex::new(Vec::new());

/// Records a thread of this process that may still be inside the engine (a close stuck behind a long statement), so
/// that [`abandoned_running`] reports it until it finishes.
pub fn track_running(handle: JoinHandle<()>) {
    if let Ok(mut abandoned) = ABANDONED.lock() {
        abandoned.push((std::process::id(), handle));
    }
}

/// Whether an abandoned engine thread of this process is still running.
///
/// A normal interpreter exit must not tear the process down under such a thread, so the caller leaves with a hard
/// exit instead.
#[must_use]
pub fn abandoned_running() -> bool {
    let pid = std::process::id();
    ABANDONED.lock().is_ok_and(|mut abandoned| {
        abandoned.retain(|(owner, handle)| *owner != pid || !handle.is_finished());
        abandoned.iter().any(|(owner, _)| *owner == pid)
    })
}

#[derive(Debug, Clone)]
pub enum Storage {
    Memory,
    Directory(PathBuf),
}

#[derive(Debug, Clone)]
pub struct EngineOptions {
    pub storage: Storage,
    // boots a memory database from a derived cluster seed instead of running initdb
    pub seed: Option<Arc<Seed>>,
    // fsync, full_page_writes and synchronous_commit off; for memory and temporary instances only
    pub fast: bool,
}

pub type Sink = Box<dyn FnMut(&[u8]) -> io::Result<()> + Send>;

enum Cmd {
    Stream {
        request: Vec<u8>,
        sink: Sink,
        reply: Sender<Result<()>>,
    },
    Raw {
        request: Vec<u8>,
        reply: Sender<Result<Vec<u8>>>,
    },
    Backup {
        reply: Sender<Result<Vec<u8>>>,
    },
    Close {
        reply: Sender<Result<()>>,
    },
}

#[derive(Debug)]
pub struct Engine {
    tx: Mutex<Option<Sender<Cmd>>>,
    thread: Mutex<Option<JoinHandle<()>>>,
    // the process that started the thread; in a forked child the thread does not exist
    pid: u32,
    // set when the engine failed at the protocol level: its session state is then unknown, and every later call
    // refuses with the reason (a memory database cannot be restarted without losing its data)
    unusable: Mutex<Option<String>>,
}

fn open(options: &EngineOptions) -> Result<Oliphaunt> {
    let mut builder = Oliphaunt::builder().extensions(runtime::extensions());
    if options.fast {
        builder = builder.startup_gucs(
            runtime::FAST_GUCS
                .iter()
                .map(|(k, v)| ((*k).to_owned(), (*v).to_owned())),
        );
    }
    match &options.storage {
        Storage::Memory => {
            if let Some(seed) = &options.seed {
                builder = builder.seed(ClusterSeed::new(Arc::clone(&seed.archive), &seed.manifest));
            }
        }
        Storage::Directory(directory) => {
            builder = builder.storage(DatabaseStorage::Directory(directory.clone()));
        }
    }
    builder
        .open()
        .map_err(|error| match (&options.storage, error.storage_error()) {
            (Storage::Directory(directory), Some(details))
                if details.code() == StorageErrorCode::Busy =>
            {
                HostError::Locked {
                    directory: directory.clone(),
                    pid: owner::live_owner(directory),
                }
            }
            _ => HostError::from(error),
        })
}

// The engine's close stops the backend the way a process exit does: it writes no shutdown checkpoint and leaves the
// control file as a crash would, so the next open replays the WAL. Replaying a sequence's log record skips the 32 values
// it pre-logged (an identity column went 1, 2, 3, then 34 after a reopen). A checkpoint just before the close puts the
// whole state in the files, so nothing is left to replay. It is best effort: a close never fails over it.
fn checkpoint(db: &mut Oliphaunt) {
    let _ = db.exec_protocol_raw(simple_query("CHECKPOINT"));
}

fn serve(db: &mut Oliphaunt, rx: &std::sync::mpsc::Receiver<Cmd>, checkpoint_on_close: bool) {
    while let Ok(cmd) = rx.recv() {
        match cmd {
            Cmd::Stream {
                request,
                sink,
                reply,
            } => {
                let result = db
                    .exec_protocol_raw_stream(request, sink)
                    .map_err(|error| HostError::Engine(format!("{error:?}")));
                let _ = reply.send(result);
            }
            Cmd::Raw { request, reply } => {
                let _ = reply.send(db.exec_protocol_raw(request).map_err(Into::into));
            }
            Cmd::Backup { reply } => {
                let _ = reply.send(db.backup().map_err(Into::into));
            }
            Cmd::Close { reply } => {
                if checkpoint_on_close {
                    checkpoint(db);
                }
                let _ = reply.send(db.close().map_err(Into::into));
                return;
            }
        }
    }
    // every sender is gone without a Close (an abandon, or a dropped engine): still shut the engine down, but never
    // spend a checkpoint on it
    let _ = db.close();
}

impl Engine {
    /// Opens the database on a new engine thread and returns once it accepts commands.
    ///
    /// # Errors
    /// When the thread cannot start or the engine cannot open the database (a held directory is `Locked`).
    pub fn start(options: EngineOptions) -> Result<Self> {
        let (tx, rx) = channel::<Cmd>();
        let (ready_tx, ready_rx) = channel::<Result<()>>();
        // only a directory outlives the instance, so only a directory gets the checkpoint at close
        let persistent = matches!(options.storage, Storage::Directory(_));
        let thread = thread::Builder::new()
            .name("rowx-engine".into())
            .stack_size(ENGINE_STACK)
            .spawn(move || {
                let mut db = match open(&options) {
                    Ok(db) => db,
                    Err(error) => {
                        let _ = ready_tx.send(Err(error));
                        return;
                    }
                };
                let _ = ready_tx.send(Ok(()));
                serve(&mut db, &rx, persistent);
            })?;
        let ready = ready_rx
            .recv()
            .map_err(|_| HostError::EngineGone)
            .and_then(|result| result);
        if let Err(error) = ready {
            let _ = thread.join();
            return Err(error);
        }
        Ok(Self {
            tx: Mutex::new(Some(tx)),
            thread: Mutex::new(Some(thread)),
            pid: std::process::id(),
            unusable: Mutex::new(None),
        })
    }

    /// Records that the engine can no longer be trusted. The first reason wins.
    pub fn mark_unusable(&self, reason: &str) {
        if let Ok(mut unusable) = self.unusable.lock() {
            unusable.get_or_insert_with(|| reason.to_owned());
        }
    }

    #[must_use]
    pub fn unusable_reason(&self) -> Option<String> {
        self.unusable.lock().ok().and_then(|reason| reason.clone())
    }

    fn sender(&self) -> Result<Sender<Cmd>> {
        let guard = self.tx.lock().map_err(|_| HostError::EngineGone)?;
        guard.as_ref().cloned().ok_or(HostError::EngineGone)
    }

    fn call<T>(&self, build: impl FnOnce(Sender<Result<T>>) -> Cmd) -> Result<T> {
        if std::process::id() != self.pid {
            return Err(HostError::ForkedProcess);
        }
        if let Some(reason) = self.unusable_reason() {
            return Err(HostError::Unusable(reason));
        }
        let (reply, rx) = channel();
        self.sender()?
            .send(build(reply))
            .map_err(|_| HostError::EngineGone)?;
        rx.recv().map_err(|_| HostError::EngineGone)?
    }

    #[must_use]
    pub fn is_running(&self) -> bool {
        self.tx.lock().is_ok_and(|guard| guard.is_some())
            && self
                .thread
                .lock()
                .is_ok_and(|thread| thread.as_ref().is_some_and(|handle| !handle.is_finished()))
    }

    /// Runs frontend protocol bytes, streaming response chunks to `sink` as they arrive.
    ///
    /// # Errors
    /// When the engine fails or the sink returns an error.
    pub fn stream(&self, request: Vec<u8>, sink: Sink) -> Result<()> {
        self.call(|reply| Cmd::Stream {
            request,
            sink,
            reply,
        })
    }

    /// # Errors
    /// When the engine fails.
    pub fn raw(&self, request: Vec<u8>) -> Result<Vec<u8>> {
        self.call(|reply| Cmd::Raw { request, reply })
    }

    /// Takes a physical backup of the live instance. The instance keeps running.
    ///
    /// # Errors
    /// When the engine fails.
    pub fn backup(&self) -> Result<Vec<u8>> {
        self.call(|reply| Cmd::Backup { reply })
    }

    /// Lets go of the engine without waiting for it: the command channel is dropped and the thread is left running (tracked by [`abandoned_running`]), so a
    /// running statement finishes (or dies with the process) on its own. A later `close` finds nothing to wait for.
    pub fn abandon(&self) {
        if std::process::id() != self.pid {
            return;
        }
        if let Ok(mut tx) = self.tx.lock() {
            tx.take();
        }
        let handle = self.thread.lock().ok().and_then(|mut thread| thread.take());
        if let Some(handle) = handle
            && !handle.is_finished()
        {
            track_running(handle);
        }
    }

    /// Cleanly shuts the engine down and joins its thread. A second call, or a call from a forked child, does nothing.
    ///
    /// # Errors
    /// When the engine fails to shut down.
    pub fn close(&self) -> Result<()> {
        if std::process::id() != self.pid {
            // the thread exists only in the parent: forget its handle instead of joining it
            if let Some(handle) = self.thread.lock().ok().and_then(|mut thread| thread.take()) {
                std::mem::forget(handle);
            }
            return Ok(());
        }
        let Some(tx) = self.tx.lock().map_err(|_| HostError::EngineGone)?.take() else {
            return Ok(());
        };
        let (reply, rx) = channel();
        let result = if tx.send(Cmd::Close { reply }).is_ok() {
            rx.recv().map_err(|_| HostError::EngineGone)?
        } else {
            Ok(())
        };
        drop(tx);
        let thread = self
            .thread
            .lock()
            .map_err(|_| HostError::EngineGone)?
            .take();
        if let Some(thread) = thread {
            let _ = thread.join();
        }
        result
    }
}

impl Drop for Engine {
    fn drop(&mut self) {
        let _ = self.close();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_abandoned_thread_counts_until_it_finishes() {
        assert!(!abandoned_running());
        let (release, wait) = channel::<()>();
        let handle = thread::spawn(move || {
            let _ = wait.recv();
        });
        if let Ok(mut abandoned) = ABANDONED.lock() {
            abandoned.push((std::process::id(), handle));
        }
        assert!(abandoned_running());
        // a thread of another process (a fork's inherited entry) never counts
        if let Ok(mut abandoned) = ABANDONED.lock() {
            abandoned.push((std::process::id().wrapping_add(1), thread::spawn(|| {})));
        }
        drop(release);
        while abandoned_running() {
            thread::yield_now();
        }
        assert!(!abandoned_running());
    }
}

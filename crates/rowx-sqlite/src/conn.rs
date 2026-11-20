use std::fmt;
use std::sync::Arc;
use std::sync::mpsc::{Receiver, Sender, channel};
use std::thread;
use std::time::Duration;

use rowx_core::error::{CoreError, TaskStopped};
use rowx_core::state::{Phase, State};
use rusqlite::config::DbConfig;
use rusqlite::limits::Limit;
use rusqlite::{InterruptHandle, OpenFlags};
use tokio::sync::oneshot;

use crate::cancel::{Shared, SqliteBackend};
use crate::dsn::{OpenMode, OpenOptions, Target};
use crate::error::Error;
use crate::timeout::Timer;

pub const DEFAULT_BUSY_TIMEOUT: Duration = Duration::from_secs(5);
pub const DEFAULT_STATEMENT_CACHE_SIZE: usize = 256;

/// Everything a connection is opened with.
#[derive(Debug, Clone)]
pub struct ConnectOptions {
    pub open: OpenOptions,
    /// Always set, from `connect_timeout`.
    pub busy_timeout: Duration,
    /// A statement running longer is interrupted and fails with SQLSTATE `57014`.
    pub statement_timeout: Option<Duration>,
    pub statement_cache: usize,
    /// Opens the file read-only and sets `PRAGMA query_only = ON`.
    pub read_only: bool,
    /// The checker reading a file it does not trust: `trusted_schema = OFF` and the defensive flag.
    pub untrusted: bool,
}

impl ConnectOptions {
    #[must_use]
    pub const fn new(open: OpenOptions) -> Self {
        Self {
            open,
            busy_timeout: DEFAULT_BUSY_TIMEOUT,
            statement_timeout: None,
            statement_cache: DEFAULT_STATEMENT_CACHE_SIZE,
            read_only: false,
            untrusted: false,
        }
    }

    // `read_only` wins over the mode the caller picked
    fn flags(&self) -> OpenFlags {
        if self.read_only {
            OpenOptions::new(self.open.target.clone(), OpenMode::ReadOnly).flags()
        } else {
            self.open.flags()
        }
    }

    // a read-only handle never switches the journal mode; see `apply_pragmas`
    fn is_read_only(&self) -> bool {
        self.read_only || self.open.mode == OpenMode::ReadOnly
    }
}

/// What the worker thread owns and runs jobs against.
#[derive(Debug)]
pub(crate) struct Ctx {
    pub(crate) conn: rusqlite::Connection,
    pub(crate) shared: Arc<Shared>,
    pub(crate) state: Arc<State>,
    pub(crate) timer: Option<Timer>,
}

impl Ctx {
    // Run before every reply, so the join in `settle` implies the worker is quiescent: when SQLite says
    // the connection is back in autocommit (an interrupted write rolled its transaction back), the
    // conservative "maybe in a transaction" mark a cancel left behind is cleared.
    pub(crate) fn reconcile(&self) {
        if self.conn.is_autocommit() && self.state.maybe_in_txn() {
            self.state.clear_maybe_in_txn();
        }
    }
}

type Work = Box<dyn FnOnce(&mut Ctx) + Send>;

enum Job {
    Run(Work),
    Close(oneshot::Sender<()>),
}

struct Ready {
    interrupt: Arc<InterruptHandle>,
    variable_limit: usize,
    journal_mode: Box<str>,
}

// Dropped when the worker thread ends for any reason. A thread that unwinds (it never reached the end
// of its loop) also breaks the state, and the jobs it was running drop their reply senders, which
// callers see as `TaskStopped`. This is not `thread::panicking()`, which `resume_unwind` leaves false.
struct Lifeline {
    shared: Arc<Shared>,
    state: Arc<State>,
    finished: bool,
}

impl Drop for Lifeline {
    fn drop(&mut self) {
        self.shared.mark_dead();
        if !self.finished {
            self.state.mark_broken();
        }
    }
}

// the pragma answers with the mode the database is in afterwards, which is the old one when the
// switch did not happen
fn require_wal(mode: &str) -> Result<(), Error> {
    if mode.eq_ignore_ascii_case("wal") {
        return Ok(());
    }
    Err(journal_error(mode))
}

fn journal_error(found: &str) -> Error {
    Error::from_code(
        rusqlite::ffi::SQLITE_ERROR,
        format!("could not switch the journal mode to WAL; the database stays in {found} mode"),
        None,
    )
}

// Every pragma is set explicitly, because the bundled build changes SQLite's defaults
// (`SQLITE_DEFAULT_FOREIGN_KEYS=1`).
fn apply_pragmas(conn: &rusqlite::Connection, options: &ConnectOptions) -> Result<Box<str>, Error> {
    conn.busy_timeout(options.busy_timeout)?;
    // a double-quoted name that matches no column is an error, not a string literal
    conn.set_db_config(DbConfig::SQLITE_DBCONFIG_DQS_DML, false)?;
    conn.set_db_config(DbConfig::SQLITE_DBCONFIG_DQS_DDL, false)?;
    if options.untrusted {
        conn.pragma_update(None, "trusted_schema", false)?;
        conn.set_db_config(DbConfig::SQLITE_DBCONFIG_DEFENSIVE, true)?;
    }
    conn.pragma_update(None, "foreign_keys", true)?;
    conn.pragma_update(None, "recursive_triggers", false)?;
    conn.pragma_update(None, "synchronous", "FULL")?;
    let mode: String = if matches!(options.open.target, Target::Memory) || options.is_read_only() {
        // `:memory:` stays `memory`. A read-only handle cannot switch the mode, and a read-only open
        // of a WAL file (or of a file in another mode) must still succeed, so it takes what it finds.
        conn.pragma_query_value(None, "journal_mode", |row| row.get(0))?
    } else {
        conn.pragma_update_and_check(None, "journal_mode", "WAL", |row| row.get(0))?
    };
    let is_file = matches!(options.open.target, Target::File(_));
    if is_file && !options.is_read_only() {
        require_wal(&mode)?;
    }
    if options.read_only {
        conn.pragma_update(None, "query_only", true)?;
    }
    Ok(mode.into())
}

fn open(
    options: &ConnectOptions,
    shared: Arc<Shared>,
    state: Arc<State>,
) -> Result<(Ctx, Ready), Error> {
    let conn =
        rusqlite::Connection::open_with_flags(options.open.target.open_path(), options.flags())?;
    let journal_mode = apply_pragmas(&conn, options)?;
    conn.set_prepared_statement_cache_capacity(options.statement_cache);
    let variable_limit = usize::try_from(conn.limit(Limit::SQLITE_LIMIT_VARIABLE_NUMBER)?)
        .map_err(|_| Error::Interface("SQLite reported a negative variable limit".to_owned()))?;
    let interrupt = Arc::new(conn.get_interrupt_handle());
    let timer = options
        .statement_timeout
        .map(|timeout| {
            let handle = Arc::clone(&interrupt);
            Timer::new(timeout, move || handle.interrupt()).map_err(|err| {
                Error::Interface(format!(
                    "could not start the statement timeout timer: {err}"
                ))
            })
        })
        .transpose()?;
    Ok((
        Ctx {
            conn,
            shared,
            state,
            timer,
        },
        Ready {
            interrupt,
            variable_limit,
            journal_mode,
        },
    ))
}

fn work(
    options: &ConnectOptions,
    shared: Arc<Shared>,
    state: Arc<State>,
    jobs: &Receiver<Job>,
    ready: oneshot::Sender<Result<Ready, Error>>,
) {
    let mut lifeline = Lifeline {
        shared: Arc::clone(&shared),
        state: Arc::clone(&state),
        finished: false,
    };
    let mut ctx = match open(options, shared, state) {
        Ok((ctx, info)) => {
            if ready.send(Ok(info)).is_err() {
                lifeline.finished = true;
                return;
            }
            ctx
        }
        Err(err) => {
            let _ = ready.send(Err(err));
            lifeline.finished = true;
            return;
        }
    };
    // a dropped job sender ends the loop, so a connection that is garbage-collected without `close()`
    // also exits and closes its database
    let mut closing = None;
    while let Ok(job) = jobs.recv() {
        match job {
            Job::Run(run) => run(&mut ctx),
            Job::Close(done) => {
                closing = Some(done);
                break;
            }
        }
    }
    drop(ctx);
    lifeline.finished = true;
    if let Some(done) = closing {
        let _ = done.send(());
    }
}

/// One SQLite connection. A worker thread owns the database handle; every call is a job on its channel
/// and resolves through a oneshot, so no SQLite call ever runs on a tokio worker.
pub struct Connection {
    jobs: Option<Sender<Job>>,
    shared: Arc<Shared>,
    state: Arc<State>,
    backend: SqliteBackend,
    variable_limit: usize,
    journal_mode: Box<str>,
}

impl fmt::Debug for Connection {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Connection")
            .field("open", &self.jobs.is_some())
            .field("variable_limit", &self.variable_limit)
            .field("journal_mode", &self.journal_mode)
            .finish_non_exhaustive()
    }
}

impl Connection {
    /// Opens on the worker thread, so this never blocks tokio.
    ///
    /// # Errors
    /// `Interface` when a thread cannot be spawned, `Sqlite` for an open or pragma failure (a failed
    /// switch to WAL included), `Stopped` when the worker died while opening.
    pub async fn connect(options: &ConnectOptions) -> Result<Self, Error> {
        let shared = Arc::new(Shared::default());
        let state = Arc::new(State::default());
        let (jobs, inbox) = channel();
        let (ready, reply) = oneshot::channel();
        let owned = options.clone();
        let (worker_shared, worker_state) = (Arc::clone(&shared), Arc::clone(&state));
        // the handle is dropped, not joined: the worker closes the database on its own way out
        thread::Builder::new()
            .name("rowx-sqlite".to_owned())
            .spawn(move || work(&owned, worker_shared, worker_state, &inbox, ready))
            .map_err(|err| {
                Error::Interface(format!("could not start the SQLite worker thread: {err}"))
            })?;
        let info = reply.await.map_err(|_| TaskStopped)??;
        Ok(Self {
            jobs: Some(jobs),
            backend: SqliteBackend::new(info.interrupt, Arc::clone(&shared)),
            shared,
            state,
            variable_limit: info.variable_limit,
            journal_mode: info.journal_mode,
        })
    }

    #[must_use]
    pub const fn state(&self) -> &Arc<State> {
        &self.state
    }

    #[must_use]
    pub fn backend(&self) -> SqliteBackend {
        self.backend.clone()
    }

    /// `SQLITE_LIMIT_VARIABLE_NUMBER`, which the binding checks parameter counts against.
    #[must_use]
    pub const fn variable_limit(&self) -> usize {
        self.variable_limit
    }

    /// The journal mode the connection ended up in (`wal` for a read-write file, `memory` for
    /// `:memory:`; a read-only handle reports what the file is in).
    #[must_use]
    pub fn journal_mode(&self) -> &str {
        &self.journal_mode
    }

    /// `true` when the connection can no longer be used.
    #[must_use]
    pub fn is_broken(&self) -> bool {
        !self.shared.is_alive() || matches!(self.state.phase(), Phase::Broken)
    }

    /// How many rows the worker has read from SQLite so far; tests use it to see back-pressure.
    #[cfg(feature = "test-support")]
    #[must_use]
    pub fn rows_stepped(&self) -> u64 {
        self.shared.rows_stepped()
    }

    pub(crate) fn submit(&self, job: Work) -> Result<(), Error> {
        let jobs = self.jobs.as_ref().ok_or(CoreError::Closed)?;
        jobs.send(Job::Run(job)).map_err(|_| TaskStopped)?;
        Ok(())
    }

    pub(crate) const fn shared(&self) -> &Arc<Shared> {
        &self.shared
    }

    // Runs `job` on the worker and waits for its result. The worker reconciles the transaction state
    // before it replies.
    pub(crate) async fn call<T, F>(&self, job: F) -> Result<T, Error>
    where
        T: Send + 'static,
        F: FnOnce(&mut Ctx) -> Result<T, Error> + Send + 'static,
    {
        let (reply, answer) = oneshot::channel();
        self.submit(Box::new(move |ctx| {
            let out = job(ctx);
            ctx.reconcile();
            // a caller that stopped waiting is gone, which is not the worker's problem
            let _ = reply.send(out);
        }))?;
        answer.await.map_err(|_| TaskStopped)?
    }

    /// A raw `sqlite3_interrupt` handle that does not set the cancel flag; tests use it to interrupt
    /// without the flag's pre-check.
    #[cfg(feature = "test-support")]
    #[must_use]
    pub fn interrupt_handle(&self) -> Arc<InterruptHandle> {
        self.backend.interrupt_handle()
    }

    /// Runs `job` against the raw handle on the worker thread. The tests use it to set up fixtures and
    /// to make the worker unwind; nothing in the driver calls it.
    ///
    /// # Errors
    /// `Closed` after `close()`, `Stopped` when the worker died.
    #[cfg(feature = "test-support")]
    pub async fn run_raw<T, F>(&self, job: F) -> Result<T, Error>
    where
        T: Send + 'static,
        F: FnOnce(&mut rusqlite::Connection) -> T + Send + 'static,
    {
        self.call(move |ctx| Ok(job(&mut ctx.conn))).await
    }

    /// Runs one control statement that returns no rows (`BEGIN`, `COMMIT`, `SAVEPOINT`, ...). It is not
    /// for scripts: a second statement is an error.
    ///
    /// # Errors
    /// `Sqlite` for a failure SQLite reports, `Interface` for a script or a statement that returns rows.
    pub async fn simple(&self, sql: &str) -> Result<(), Error> {
        let sql = sql.to_owned();
        self.call(move |ctx| {
            crate::exec::execute_sql(ctx, &sql, &[])?;
            Ok(())
        })
        .await
    }

    /// Runs statements without looking at the cancel flag. An abandoned operation cleans up with this
    /// while its cancel is still pending.
    ///
    /// # Errors
    /// `Sqlite` for a failing statement, `Closed` after `close()`, `Stopped` when the worker died.
    pub async fn simple_uncancelled(&self, sql: &str) -> Result<(), Error> {
        let sql = sql.to_owned();
        self.call(move |ctx| {
            ctx.conn.execute_batch(&sql)?;
            Ok(())
        })
        .await
    }

    /// # Errors
    /// `Closed` after `close()`, `Stopped` when the worker died.
    pub async fn is_autocommit(&self) -> Result<bool, Error> {
        self.call(|ctx| Ok(ctx.conn.is_autocommit())).await
    }

    /// A cheap round trip through the worker and the database.
    ///
    /// # Errors
    /// `Sqlite` when the database cannot answer, `Stopped` when the worker died.
    pub async fn ping(&self) -> Result<(), Error> {
        self.call(|ctx| {
            ctx.conn
                .query_row("SELECT 1", [], |row| row.get::<_, i64>(0))?;
            Ok(())
        })
        .await
    }

    /// Puts the session back to a clean state; `rollback` is set when it may be inside a transaction.
    ///
    /// # Errors
    /// `Sqlite` when the rollback fails.
    pub async fn reset(&self, rollback: bool) -> Result<(), Error> {
        self.call(move |ctx| {
            if rollback && !ctx.conn.is_autocommit() {
                ctx.conn.execute_batch("ROLLBACK")?;
            }
            Ok(())
        })
        .await
    }

    /// Drops the prepared statement cache. SQLite re-prepares a stale statement itself, so this is only
    /// for a pool that wants a connection to let go of its statements.
    ///
    /// # Errors
    /// `Closed` after `close()`, `Stopped` when the worker died.
    pub async fn forget_statements(&self) -> Result<(), Error> {
        self.call(|ctx| {
            ctx.conn.flush_prepared_statement_cache();
            Ok(())
        })
        .await
    }

    /// Closes the database on the worker and resolves once it is closed. Idempotent. The worker's
    /// `JoinHandle` was dropped at connect, so nothing is joined.
    pub async fn close(&mut self) {
        self.state.close();
        let Some(jobs) = self.jobs.take() else {
            return;
        };
        // `Job::Close` queues behind a running job, and a closed state never sends a cancel, so wake a
        // worker that is stepping or parked on a full stream channel before waiting for it
        self.shared.request_cancel();
        self.backend.interrupt();
        let (done, closed) = oneshot::channel();
        if jobs.send(Job::Close(done)).is_ok() {
            // a worker that already died answers by dropping the sender; the database is gone either way
            let _ = closed.await;
        }
        self.shared.mark_dead();
    }
}

#[cfg(test)]
mod tests {
    use super::require_wal;
    use crate::error::Error;

    #[test]
    fn wal_is_accepted_in_any_case() {
        assert_eq!(require_wal("wal"), Ok(()));
        assert_eq!(require_wal("WAL"), Ok(()));
    }

    #[test]
    fn a_pragma_that_returns_the_old_mode_is_an_error() {
        for old in ["delete", "truncate", "persist", "memory", "off"] {
            let err = require_wal(old).err();
            assert!(
                matches!(&err, Some(Error::Sqlite { name: "SQLITE_ERROR", message, .. })
                    if message.contains(old) && message.contains("WAL")),
                "{old}: {err:?}"
            );
            assert_eq!(err.and_then(|e| e.sqlstate()), Some("HY000"));
        }
    }
}

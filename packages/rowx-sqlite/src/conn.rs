use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, Mutex as StdMutex};
use std::time::Duration;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use pyo3_async_runtimes::tokio::future_into_py;
use rowx_core::cancel::Backend;
use rowx_core::error::CoreError;
use rowx_py_core::errors::interface_error;
use rowx_py_core::runtime;
use rowx_py_core::time::seconds;
use rowx_sqlite::cancel::Cancel;
use rowx_sqlite::conn::{ConnectOptions, Connection as SqliteConnection};
use rowx_sqlite::dsn::{self, OpenMode, OpenOptions, Target};
use rowx_sqlite::error::Error;
use rowx_sqlite::pool::{Pool as SqlitePool, Pooled, SqliteConn, SqliteManager};
use tokio::runtime::Handle;
use tokio::sync::Mutex;

use crate::errors::{Failure, SqliteError, to_py_err};
use crate::exec::{self, Depth, FetchArgs};
use crate::named::RowShape;
use crate::stream::{RowStream, StreamArgs};

/// How a call reaches its connection.
pub type Access = rowx_py_core::exec::Access<SqliteManager>;

/// What an operation, and a stream that outlives its call, share with the connection object.
#[derive(Debug)]
pub(crate) struct Core {
    pub(crate) cancel: Cancel,
    pub(crate) conn: Arc<Mutex<SqliteConnection>>,
    pub(crate) access: Access,
    pub(crate) variable_limit: usize,
    // a read-only connection cannot take the default `BEGIN IMMEDIATE`
    pub(crate) read_only: bool,
    // the transaction helper's depth as the caller sees it: SQLite ends a transaction on its own after an
    // interrupt, which resets the shared state, so the difference between the two marks an aborted one
    open: AtomicU32,
    // what `PRAGMA query_only` was before a read-only transaction turned it on
    restore: StdMutex<Option<bool>>,
}

impl Core {
    #[must_use]
    pub(crate) fn new(conn: SqliteConnection, handle: Handle, read_only: bool) -> Self {
        Self::over(&SqliteConn::new(conn, read_only), handle, Access::default())
    }

    // a connection of a pool, claimed through the lease in `access`
    #[must_use]
    pub(crate) fn over(conn: &SqliteConn, handle: Handle, access: Access) -> Self {
        Self {
            cancel: Cancel {
                state: Arc::clone(conn.state()),
                backend: conn.backend(),
                handle,
            },
            variable_limit: conn.variable_limit(),
            read_only: conn.read_only(),
            conn: Arc::clone(conn.conn()),
            access,
            open: AtomicU32::new(0),
            restore: StdMutex::new(None),
        }
    }

    /// The core of a checkout, claimed through its lease.
    ///
    /// # Errors
    /// `Closed` when the checkout was already released.
    pub(crate) fn pooled(pool: &SqlitePool, pooled: &Pooled) -> Result<Self, CoreError> {
        let access = Access {
            lease: Some(pooled.lease()),
            pool: Some(pool.clone()),
        };
        let handle = Handle::try_current().map_err(|_| CoreError::Closed)?;
        Ok(Self::over(pooled.conn()?, handle, access))
    }

    #[must_use]
    pub(crate) fn depth(&self) -> u32 {
        self.open.load(Ordering::Acquire)
    }

    /// The caller has an open `transaction()` that SQLite already ended.
    #[must_use]
    pub(crate) fn aborted(&self) -> bool {
        self.depth() > 0 && !self.cancel.state.maybe_in_txn()
    }

    /// A transaction or savepoint was opened; returns the new depth.
    pub(crate) fn enter(&self) -> u32 {
        self.cancel.state.enter_txn();
        self.open.fetch_add(1, Ordering::AcqRel).saturating_add(1)
    }

    /// A savepoint was released; returns the new depth.
    pub(crate) fn leave(&self) -> u32 {
        self.cancel.state.leave_txn();
        let mut current = self.open.load(Ordering::Acquire);
        loop {
            let next = current.saturating_sub(1);
            match self.open.compare_exchange_weak(
                current,
                next,
                Ordering::AcqRel,
                Ordering::Acquire,
            ) {
                Ok(_) => return next,
                Err(actual) => current = actual,
            }
        }
    }

    /// The outermost transaction ended.
    pub(crate) fn clear(&self) {
        self.cancel.state.clear_maybe_in_txn();
        self.open.store(0, Ordering::Release);
    }

    // a value that is still pending is the original: the pragma now reads the state a transaction left
    pub(crate) fn remember_query_only(&self, prior: bool) {
        if let Ok(mut slot) = self.restore.lock() {
            slot.get_or_insert(prior);
        }
    }

    pub(crate) fn take_query_only(&self) -> Option<bool> {
        self.restore.lock().ok().and_then(|mut slot| slot.take())
    }

    /// # Errors
    /// `InterfaceError` when a statement has more parameters than SQLite allows.
    pub(crate) fn check_params(&self, py: Python<'_>, count: usize) -> PyResult<()> {
        if count > self.variable_limit {
            return Err(interface_error(
                py,
                &format!(
                    "the statement has {count} parameters but this SQLite connection allows at most {} (SQLITE_LIMIT_VARIABLE_NUMBER)",
                    self.variable_limit
                ),
            ));
        }
        Ok(())
    }
}

#[pyclass(frozen, module = "rowx_sqlite._core", name = "Connection")]
#[derive(Debug)]
pub struct Connection {
    core: Arc<Core>,
    // the checkout of a pooled connection, until it is released or detached
    pooled: StdMutex<Option<Pooled>>,
    init_pending: AtomicBool,
    #[pyo3(get)]
    statement_cache_size: usize,
}

impl Connection {
    #[must_use]
    pub(crate) fn new(
        conn: SqliteConnection,
        handle: Handle,
        statement_cache_size: usize,
        read_only: bool,
    ) -> Self {
        Self {
            core: Arc::new(Core::new(conn, handle, read_only)),
            pooled: StdMutex::new(None),
            init_pending: AtomicBool::new(false),
            statement_cache_size,
        }
    }

    /// Wraps a checkout: its lease is what every claim on this connection is checked against.
    ///
    /// # Errors
    /// `Closed` when the checkout was already released.
    pub(crate) fn pooled(
        pooled: Pooled,
        pool: &SqlitePool,
        statement_cache_size: usize,
    ) -> Result<Self, CoreError> {
        let core = Core::pooled(pool, &pooled)?;
        let init_pending = pooled.init_pending();
        Ok(Self {
            core: Arc::new(core),
            pooled: StdMutex::new(Some(pooled)),
            init_pending: AtomicBool::new(init_pending),
            statement_cache_size,
        })
    }

    fn slot(&self) -> std::sync::MutexGuard<'_, Option<Pooled>> {
        self.pooled
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    /// Takes the checkout and revokes the lease at once, so a stream built on it cannot claim the
    /// connection again before the release has run.
    ///
    /// # Errors
    /// `Closed` when it was already released or detached.
    pub(crate) fn take_pooled(&self) -> Result<Pooled, CoreError> {
        let pooled = self.slot().take().ok_or(CoreError::Closed)?;
        self.core.cancel.state.renew_lease();
        Ok(pooled)
    }
}

impl Connection {
    pub(crate) const fn core(&self) -> &Arc<Core> {
        &self.core
    }
}

#[pymethods]
impl Connection {
    fn close<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        // closing a pooled connection gives it back
        if let Ok(pooled) = self.take_pooled() {
            return future_into_py(py, async move {
                pooled.release().await;
                Ok(())
            });
        }
        let core = Arc::clone(&self.core);
        future_into_py(py, async move {
            core.cancel.state.close();
            // an operation that is still running holds the connection for its full length, so it is
            // interrupted until it gives the connection back
            let mut conn = if let Ok(conn) = core.conn.try_lock() {
                conn
            } else {
                core.cancel
                    .backend
                    .interrupt_until_closed(core.conn.lock())
                    .await
            };
            conn.close().await;
            drop(conn);
            Ok(())
        })
    }

    // the argument list is fixed by Executor._fetch in packages/rowx-core/src/rowx_core/_executor.py
    #[allow(clippy::too_many_arguments)]
    #[pyo3(name = "_fetch")]
    fn fetch<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        params: &Bound<'py, PyList>,
        mask: Option<Vec<u8>>,
        scalar: bool,
        limit_one: bool,
        arity: Option<usize>,
        shape: Option<Py<RowShape>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let args = FetchArgs {
            sql,
            mask,
            scalar,
            limit_one,
            arity,
            shape,
        };
        exec::fetch(py, &self.core, params, args)
    }

    // the argument list is fixed by Executor._stream in packages/rowx-core/src/rowx_core/_executor.py
    #[allow(clippy::too_many_arguments)]
    #[pyo3(name = "_stream")]
    fn stream(
        &self,
        py: Python<'_>,
        sql: String,
        params: &Bound<'_, PyList>,
        mask: Option<Vec<u8>>,
        scalar: bool,
        arity: Option<usize>,
        batch_size: usize,
        shape: Option<Py<RowShape>>,
    ) -> PyResult<RowStream> {
        let args = StreamArgs {
            sql,
            mask,
            scalar,
            arity,
            batch_size,
            shape,
        };
        RowStream::new(py, &self.core, params, args)
    }

    #[pyo3(name = "_execute")]
    fn execute<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        params: &Bound<'py, PyList>,
    ) -> PyResult<Bound<'py, PyAny>> {
        exec::execute(py, &self.core, params, sql)
    }

    fn ping<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        exec::ping(py, &self.core)
    }

    // `mode` is deferred, immediate or exclusive; a read-only transaction is always deferred
    #[pyo3(name = "_begin")]
    fn begin<'py>(
        &self,
        py: Python<'py>,
        mode: Option<&str>,
        read_only: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        if self.core.depth() > 0 {
            return Err(interface_error(
                py,
                "the connection is already in a transaction; use Transaction.transaction() for a savepoint",
            ));
        }
        let sql =
            exec::begin_sql(mode, read_only, self.core.read_only).map_err(|e| to_py_err(py, &e))?;
        exec::begin(py, &self.core, sql, read_only)
    }

    // resolves to the depth, which names the savepoint
    #[pyo3(name = "_savepoint")]
    fn savepoint<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let depth = self.core.depth();
        if depth == 0 {
            return Err(interface_error(py, "there is no open transaction"));
        }
        exec::txn_step(
            py,
            &self.core,
            format!("SAVEPOINT rowx_sp_{}", depth.saturating_add(1)),
            Depth::Enter,
        )
    }

    #[pyo3(name = "_release_savepoint")]
    fn release_savepoint<'py>(&self, py: Python<'py>, n: u32) -> PyResult<Bound<'py, PyAny>> {
        exec::txn_step(
            py,
            &self.core,
            format!("RELEASE SAVEPOINT rowx_sp_{n}"),
            Depth::Leave,
        )
    }

    #[pyo3(name = "_rollback_to_savepoint")]
    fn rollback_to_savepoint<'py>(&self, py: Python<'py>, n: u32) -> PyResult<Bound<'py, PyAny>> {
        exec::txn_step(
            py,
            &self.core,
            format!("ROLLBACK TO SAVEPOINT rowx_sp_{n}"),
            Depth::Keep,
        )
    }

    #[pyo3(name = "_end_transaction")]
    fn end_transaction<'py>(&self, py: Python<'py>, commit: bool) -> PyResult<Bound<'py, PyAny>> {
        exec::end_transaction(py, &self.core, commit)
    }

    #[getter]
    fn closed(&self) -> bool {
        self.core.cancel.state.is_closed()
    }

    #[getter]
    fn variable_limit(&self) -> usize {
        self.core.variable_limit
    }

    // Cancels and settles whatever still runs on the connection, so a reset hook can use it.
    #[pyo3(name = "_settle")]
    fn settle<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let core = Arc::clone(&self.core);
        future_into_py(py, async move {
            rowx_py_core::exec::settle(&core.cancel.state, &core.cancel.backend)
                .await
                .map_err(SqliteError::from)?;
            // the settle may have sent cancels; take the flag so the hook's own work is not interrupted
            if !core.cancel.backend.is_alive().await {
                let err = if core.cancel.state.is_closed() {
                    CoreError::Closed
                } else {
                    CoreError::Broken
                };
                return Err(SqliteError::from(err).into());
            }
            Ok(())
        })
    }

    // ends an open transaction and puts `query_only` back, so a hook sees an idle connection
    fn reset<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        exec::reset(py, &self.core)
    }

    #[getter]
    fn init_pending(&self) -> bool {
        self.init_pending.load(Ordering::Acquire)
    }

    #[pyo3(name = "_clear_init_pending")]
    fn clear_init_pending(&self) {
        self.init_pending.store(false, Ordering::Release);
        if let Some(pooled) = self.slot().as_mut() {
            pooled.clear_init_pending();
        }
    }

    /// Takes the connection out of the pool for good; it then behaves like one from `connect`.
    fn detach(&self, py: Python<'_>) -> PyResult<()> {
        let taken = self.slot().take();
        taken
            .ok_or_else(|| interface_error(py, "the connection is not checked out of a pool"))?
            .detach()
            .map_err(|e| to_py_err(py, &Failure::from(e)))?;
        Ok(())
    }
}

// reads a `rowx_sqlite.ConnectOptions`; the shared Python never imports this module
pub(crate) fn options_from_py(py: Python<'_>, opts: &Bound<'_, PyAny>) -> PyResult<ConnectOptions> {
    let path: Option<String> = opts.getattr("path")?.extract()?;
    let memory: bool = opts.getattr("memory")?.extract()?;
    let target = match (path, memory) {
        (Some(path), false) if !path.is_empty() => Target::File(PathBuf::from(path)),
        (Some(_), false) => {
            return Err(interface_error(py, "the database path is empty"));
        }
        (None, true) => Target::Memory,
        (Some(_), true) => {
            return Err(interface_error(
                py,
                "path and memory are mutually exclusive",
            ));
        }
        (None, false) => {
            return Err(interface_error(
                py,
                "a database path is required (or memory=True)",
            ));
        }
    };
    let mode = match opts.getattr("mode")?.extract::<String>()?.as_str() {
        "ro" => OpenMode::ReadOnly,
        "rw" => OpenMode::ReadWrite,
        "rwc" => OpenMode::ReadWriteCreate,
        other => {
            return Err(interface_error(
                py,
                &format!("mode must be \"ro\", \"rw\" or \"rwc\", not {other:?}"),
            ));
        }
    };
    let mut options = ConnectOptions::new(OpenOptions::new(target, mode));
    let connect_timeout: Option<f64> = opts.getattr("connect_timeout")?.extract()?;
    if let Some(secs) = connect_timeout {
        options.busy_timeout = seconds("connect_timeout", secs)?;
    }
    let statement_timeout: Option<f64> = opts.getattr("statement_timeout")?.extract()?;
    // like PostgreSQL's `statement_timeout`, zero means no limit
    options.statement_timeout = statement_timeout
        .map(|secs| seconds("statement_timeout", secs))
        .transpose()?
        .filter(|limit| *limit > Duration::ZERO);
    options.statement_cache = opts.getattr("statement_cache_size")?.extract()?;
    options.read_only = opts.getattr("read_only")?.extract()?;
    Ok(options)
}

/// # Errors
/// `InterfaceError` for a DSN that is not one of the SQLite forms.
#[pyfunction]
pub fn parse_dsn<'py>(py: Python<'py>, dsn: &str) -> PyResult<Bound<'py, PyDict>> {
    let target = dsn::parse(dsn).map_err(|e| to_py_err(py, &Failure::from(Error::Dsn(e))))?;
    let out = PyDict::new(py);
    match target {
        Target::Memory => {
            out.set_item("path", py.None())?;
            out.set_item("memory", true)?;
        }
        Target::File(path) => {
            out.set_item("path", path.to_string_lossy())?;
            out.set_item("memory", false)?;
        }
    }
    Ok(out)
}

/// # Errors
/// `InterfaceError` or `ValueError` for bad options; open failures surface when the future is awaited.
#[pyfunction]
pub fn connect<'py>(py: Python<'py>, options: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let options = options_from_py(py, options)?;
    let handle = runtime::handle()?;
    future_into_py(py, async move {
        let conn = SqliteConnection::connect(&options)
            .await
            .map_err(SqliteError::from)?;
        let read_only = options.is_read_only();
        Ok(Connection::new(
            conn,
            handle,
            options.statement_cache,
            read_only,
        ))
    })
}

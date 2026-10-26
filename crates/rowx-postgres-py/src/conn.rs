use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex as StdMutex};
use std::time::Duration;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use pyo3_async_runtimes::tokio::future_into_py;
use rowx_core::cancel::Backend;
use rowx_core::error::CoreError;
use rowx_core::state::State;
use rowx_postgres::cancel::{Cancel, PgBackend};
use rowx_postgres::conn::Connection as PgConnection;
use rowx_postgres::dsn::ConnectOptions;
use rowx_postgres::error::Error;
use rowx_postgres::pool::{Pool as PgPool, Pooled};
use tokio::runtime::Handle;
use tokio::sync::Mutex;

use crate::errors::{PgError, interface_error, to_py_err};
use crate::exec::{self, Access, FetchArgs};
use crate::stream::{RowStream, StreamArgs};
use rowx_py_core::runtime;

// how often a running operation is asked again to stop while its connection is settled
const SETTLE_RECANCEL: Duration = Duration::from_millis(100);

#[pyclass(frozen, module = "rowx._core.postgres", name = "Connection")]
#[derive(Debug)]
pub struct Connection {
    pub(crate) cancel: Cancel,
    access: Access,
    // the checkout of a pooled connection, until it is released or detached
    pooled: StdMutex<Option<Pooled>>,
    reset_done: Option<Arc<AtomicBool>>,
    init_pending: AtomicBool,
    #[pyo3(get)]
    statement_cache_size: usize,
}

impl Connection {
    #[must_use]
    pub fn new(conn: PgConnection, handle: Handle, statement_cache_size: usize) -> Self {
        Self {
            cancel: Cancel {
                state: Arc::new(State::default()),
                backend: PgBackend {
                    target: conn.cancel_target(),
                    conn: Arc::new(Mutex::new(conn)),
                },
                handle,
            },
            access: Access::default(),
            pooled: StdMutex::new(None),
            reset_done: None,
            init_pending: AtomicBool::new(false),
            statement_cache_size,
        }
    }

    /// Wraps a checkout: its lease is what every claim on this connection is checked against.
    ///
    /// # Errors
    /// `Closed` when the checkout was already released.
    pub fn pooled(
        pooled: Pooled,
        pool: PgPool,
        statement_cache_size: usize,
    ) -> Result<Self, CoreError> {
        let conn = pooled.conn()?;
        let cancel = conn.cancel.clone();
        let reset_done = Arc::clone(&conn.reset_done);
        let access = Access {
            lease: Some(pooled.lease()),
            pool: Some(pool),
        };
        let init_pending = pooled.init_pending();
        Ok(Self {
            cancel,
            access,
            pooled: StdMutex::new(Some(pooled)),
            reset_done: Some(reset_done),
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
    pub fn take_pooled(&self, reset_done: bool) -> Result<Pooled, CoreError> {
        let pooled = self.slot().take().ok_or(CoreError::Closed)?;
        if reset_done && let Some(flag) = &self.reset_done {
            flag.store(true, Ordering::Release);
        }
        self.cancel.state.renew_lease();
        Ok(pooled)
    }
}

#[pymethods]
impl Connection {
    fn close<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        // closing a pooled connection gives it back
        if let Ok(pooled) = self.take_pooled(false) {
            return future_into_py(py, async move {
                pooled.release().await;
                Ok(())
            });
        }
        let cancel = self.cancel.clone();
        future_into_py(py, async move {
            let running = cancel.state.is_busy();
            cancel.state.close();
            if running {
                // an operation that is still running would hold the lock for its full length;
                // a failed request is fine because dropping the client ends the query anyway
                let _ = cancel.backend.target.request().await;
            }
            cancel.backend.conn.lock().await.close().await;
            Ok(())
        })
    }

    // the argument list is fixed by the binding contract in docs/design/driver.md, execution path
    #[allow(clippy::too_many_arguments)]
    #[pyo3(name = "_fetch")]
    fn fetch<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        params: &Bound<'py, PyList>,
        mask: Option<Vec<bool>>,
        scalar: bool,
        limit_one: bool,
        arity: Option<usize>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let args = FetchArgs {
            sql,
            mask,
            scalar,
            limit_one,
            arity,
        };
        exec::fetch(py, &self.cancel, &self.access, params, args)
    }

    // the argument list is fixed by the binding contract in docs/design/driver.md, streaming
    #[allow(clippy::too_many_arguments)]
    #[pyo3(name = "_stream")]
    fn stream(
        &self,
        py: Python<'_>,
        sql: String,
        params: &Bound<'_, PyList>,
        mask: Option<Vec<bool>>,
        scalar: bool,
        arity: Option<usize>,
        batch_size: usize,
    ) -> PyResult<RowStream> {
        let args = StreamArgs {
            sql,
            mask,
            scalar,
            arity,
            batch_size,
        };
        RowStream::new(py, &self.cancel, &self.access, params, args)
    }

    #[pyo3(name = "_execute")]
    fn execute<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        params: &Bound<'py, PyList>,
    ) -> PyResult<Bound<'py, PyAny>> {
        exec::execute(py, &self.cancel, &self.access, params, sql)
    }

    fn ping<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        exec::simple(py, &self.cancel, &self.access, String::new(), false)
    }

    fn reset<'py>(&self, py: Python<'py>, query: String) -> PyResult<Bound<'py, PyAny>> {
        exec::simple(py, &self.cancel, &self.access, query, true)
    }

    #[getter]
    fn closed(&self) -> bool {
        self.cancel.state.is_closed()
    }

    /// Cancels and settles whatever still runs on the connection, so a reset hook can use it.
    #[pyo3(name = "_settle")]
    fn settle<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let cancel = self.cancel.clone();
        future_into_py(py, async move {
            let state = &cancel.state;
            if state.is_busy() {
                // an operation that is cut short may have left a transaction open
                state.mark_maybe_in_txn();
                // a cancel request only stops a statement that is running, so it is resent
                while tokio::time::timeout(SETTLE_RECANCEL, state.wait_unclaimed())
                    .await
                    .is_err()
                {
                    // a failed request is fine: the next round tries again
                    let _ = cancel.backend.send_cancel().await;
                }
            }
            state.wait_settled().await.map_err(PgError::from)?;
            Ok(())
        })
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
            .map_err(|e| to_py_err(py, &e.into()))?;
        Ok(())
    }
}

fn pairs(obj: &Bound<'_, PyAny>, name: &str) -> PyResult<Vec<(String, String)>> {
    let items = obj.getattr(name)?.call_method0("items")?;
    items.try_iter()?.map(|item| item?.extract()).collect()
}

// reads a `rowx.postgres.ConnectOptions`; the shared Python never imports this module
pub(crate) fn options_from_py(py: Python<'_>, opts: &Bound<'_, PyAny>) -> PyResult<ConnectOptions> {
    let timeout: Option<f64> = opts.getattr("connect_timeout")?.extract()?;
    let connect_timeout = timeout
        .map(|secs| {
            Duration::try_from_secs_f64(secs).map_err(|_| {
                interface_error(
                    py,
                    "connect_timeout must be a non-negative number of seconds",
                )
            })
        })
        .transpose()?;
    Ok(ConnectOptions {
        host: opts.getattr("host")?.extract()?,
        port: opts.getattr("port")?.extract()?,
        user: opts.getattr("user")?.extract()?,
        password: opts.getattr("password")?.extract()?,
        database: opts.getattr("database")?.extract()?,
        sslmode: opts.getattr("sslmode")?.extract()?,
        sslrootcert: opts.getattr("sslrootcert")?.extract()?,
        connect_timeout,
        statement_cache_size: opts.getattr("statement_cache_size")?.extract()?,
        application_name: opts.getattr("application_name")?.extract()?,
        server_settings: pairs(opts, "server_settings")?,
        extra: pairs(opts, "extra")?,
    })
}

/// # Errors
/// `InterfaceError` for a DSN that cannot be parsed or uses an unsupported setting.
#[pyfunction]
pub fn parse_dsn<'py>(py: Python<'py>, dsn: &str) -> PyResult<Bound<'py, PyDict>> {
    let opts = ConnectOptions::from_dsn(dsn).map_err(|e| to_py_err(py, &e))?;
    let out = PyDict::new(py);
    out.set_item("host", opts.host)?;
    out.set_item("port", opts.port)?;
    out.set_item("user", opts.user)?;
    out.set_item("password", opts.password)?;
    out.set_item("database", opts.database)?;
    out.set_item("sslmode", opts.sslmode)?;
    out.set_item("sslrootcert", opts.sslrootcert)?;
    out.set_item(
        "connect_timeout",
        opts.connect_timeout.map(|t| t.as_secs_f64()),
    )?;
    out.set_item("application_name", opts.application_name)?;
    out.set_item(
        "server_settings",
        opts.server_settings.into_iter().collect::<HashMap<_, _>>(),
    )?;
    out.set_item("extra", opts.extra.into_iter().collect::<HashMap<_, _>>())?;
    Ok(out)
}

/// # Errors
/// `InterfaceError` for a bad `connect_timeout`; connection failures surface when the future is awaited.
#[pyfunction]
pub fn connect<'py>(py: Python<'py>, options: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let options = options_from_py(py, options)?;
    let handle = runtime::handle()?;
    future_into_py(py, async move {
        let connecting = PgConnection::connect(&options, &handle);
        let limit = options.connect_timeout.filter(|t| !t.is_zero());
        let result = match limit {
            Some(limit) => tokio::time::timeout(limit, connecting)
                .await
                .unwrap_or_else(|_| Err(Error::Interface("connection timed out".to_owned()))),
            None => connecting.await,
        };
        let conn = result.map_err(PgError)?;
        Ok(Connection::new(conn, handle, options.statement_cache_size))
    })
}

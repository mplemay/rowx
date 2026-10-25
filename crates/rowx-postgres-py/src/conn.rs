use std::sync::Arc;
use std::time::Duration;

use pyo3::prelude::*;
use pyo3::types::PyList;
use pyo3_async_runtimes::tokio::future_into_py;
use rowx_core::state::State;
use rowx_postgres::cancel::{Cancel, PgBackend};
use rowx_postgres::conn::Connection as PgConnection;
use rowx_postgres::error::Error;
use tokio::runtime::Handle;
use tokio::sync::Mutex;

use crate::errors::{PgError, interface_error};
use crate::exec::{self, FetchArgs};
use crate::stream::{RowStream, StreamArgs};
use rowx_py_core::runtime;

#[pyclass(frozen, module = "rowx._core.postgres", name = "Connection")]
#[derive(Debug)]
pub struct Connection {
    pub(crate) cancel: Cancel,
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
            statement_cache_size,
        }
    }
}

#[pymethods]
impl Connection {
    fn close<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
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
        exec::fetch(py, &self.cancel, params, args)
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
        RowStream::new(py, &self.cancel, params, args)
    }

    #[pyo3(name = "_execute")]
    fn execute<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        params: &Bound<'py, PyList>,
    ) -> PyResult<Bound<'py, PyAny>> {
        exec::execute(py, &self.cancel, params, sql)
    }

    #[getter]
    fn closed(&self) -> bool {
        self.cancel.state.is_closed()
    }
}

/// # Errors
/// `InterfaceError` for a bad `connect_timeout`; connection failures surface when the future is awaited.
#[pyfunction]
#[pyo3(signature = (dsn, /, *, connect_timeout=None, statement_cache_size=256))]
pub fn connect(
    py: Python<'_>,
    dsn: String,
    connect_timeout: Option<f64>,
    statement_cache_size: usize,
) -> PyResult<Bound<'_, PyAny>> {
    let timeout = connect_timeout
        .map(|secs| {
            Duration::try_from_secs_f64(secs).map_err(|_| {
                interface_error(
                    py,
                    "connect_timeout must be a non-negative number of seconds",
                )
            })
        })
        .transpose()?;
    let handle = runtime::handle()?;
    future_into_py(py, async move {
        let connecting = PgConnection::connect(&dsn, &handle, statement_cache_size);
        let result = match timeout {
            Some(limit) => tokio::time::timeout(limit, connecting)
                .await
                .unwrap_or_else(|_| Err(Error::Interface("connection timed out".to_owned()))),
            None => connecting.await,
        };
        let conn = result.map_err(PgError)?;
        Ok(Connection::new(conn, handle, statement_cache_size))
    })
}

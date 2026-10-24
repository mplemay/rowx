use std::sync::Arc;
use std::time::Duration;

use pyo3::prelude::*;
use pyo3::types::PyList;
use pyo3_async_runtimes::tokio::future_into_py;
use rowx_pg::cancel::Cancel;
use rowx_pg::conn::Connection as PgConnection;
use rowx_pg::error::Error;
use rowx_pg::state::State;
use tokio::sync::Mutex;

use crate::errors::{PgError, interface_error};
use crate::exec::{self, FetchArgs};
use crate::runtime;

#[pyclass(frozen, module = "rowx._core", name = "Connection")]
#[derive(Debug)]
pub struct Connection {
    cancel: Cancel,
    #[pyo3(get)]
    statement_cache_size: usize,
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
                let _ = cancel.target.request().await;
            }
            cancel.conn.lock().await.close().await;
            Ok(())
        })
    }

    // the argument list is fixed by the binding contract in design §7.2
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
        let target = conn.cancel_target();
        Ok(Connection {
            cancel: Cancel {
                state: Arc::new(State::default()),
                target,
                conn: Arc::new(Mutex::new(conn)),
                handle,
            },
            statement_cache_size,
        })
    })
}

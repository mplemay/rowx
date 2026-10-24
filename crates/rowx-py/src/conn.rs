use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use pyo3::prelude::*;
use pyo3_async_runtimes::tokio::future_into_py;
use rowx_pg::conn::Connection as PgConnection;
use rowx_pg::error::Error;
use tokio::sync::Mutex;

use crate::errors::{PgError, interface_error};
use crate::runtime;

#[pyclass(frozen, module = "rowx._core", name = "Connection")]
#[derive(Debug)]
pub struct Connection {
    inner: Arc<Mutex<PgConnection>>,
    closed: Arc<AtomicBool>,
    // stored for the statement cache (T20); it has no effect yet
    #[pyo3(get)]
    statement_cache_size: usize,
}

#[pymethods]
impl Connection {
    fn close<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = Arc::clone(&self.inner);
        let closed = Arc::clone(&self.closed);
        future_into_py(py, async move {
            closed.store(true, Ordering::Release);
            inner.lock().await.close().await;
            Ok(())
        })
    }

    #[getter]
    fn closed(&self) -> bool {
        self.closed.load(Ordering::Acquire)
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
        let connecting = PgConnection::connect(&dsn, &handle);
        let result = match timeout {
            Some(limit) => tokio::time::timeout(limit, connecting)
                .await
                .unwrap_or_else(|_| Err(Error::Interface("connection timed out".to_owned()))),
            None => connecting.await,
        };
        let conn = result.map_err(PgError)?;
        Ok(Connection {
            inner: Arc::new(Mutex::new(conn)),
            closed: Arc::new(AtomicBool::new(false)),
            statement_cache_size,
        })
    })
}

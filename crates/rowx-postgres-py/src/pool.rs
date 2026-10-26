use std::sync::Arc;
use std::sync::atomic::AtomicBool;
use std::time::Duration;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyList;
use pyo3_async_runtimes::tokio::future_into_py;
use rowx_core::error::CoreError;
use rowx_postgres::cancel::Hook;
use rowx_postgres::error::Error;
use rowx_postgres::pool::{PgManager, Pool as PgPool, PoolOptions};
use rowx_py_core::{errors as shared, runtime};

use crate::conn::{Connection, options_from_py};
use crate::errors::{PgError, options_error, to_py_err};
use crate::exec::{self, Access, FetchArgs, watch, watch_flag};
use crate::stream::{RowStream, StreamArgs};

fn seconds(name: &str, secs: f64) -> PyResult<Duration> {
    Duration::try_from_secs_f64(secs).map_err(|_| {
        PyValueError::new_err(format!("{name} must be a non-negative number of seconds"))
    })
}

fn optional_seconds(name: &str, secs: Option<f64>) -> PyResult<Option<Duration>> {
    secs.map(|secs| seconds(name, secs)).transpose()
}

#[pyclass(frozen, module = "rowx._core.postgres", name = "Pool")]
#[derive(Debug)]
pub struct Pool {
    inner: PgPool,
    statement_cache_size: usize,
    // the pool is unusable in a forked child, whose runtime threads are gone
    pid: u32,
}

impl Pool {
    fn check_process(&self, py: Python<'_>) -> PyResult<()> {
        if std::process::id() == self.pid {
            Ok(())
        } else {
            Err(shared::core_to_py_err(py, CoreError::Forked))
        }
    }

    // the pool settings are validated in Python first; this is the backstop
    #[allow(clippy::too_many_arguments)]
    fn options(
        connect_timeout: Option<Duration>,
        min_size: usize,
        max_size: usize,
        acquire_timeout: f64,
        idle_timeout: Option<f64>,
        max_lifetime: Option<f64>,
        max_queries: Option<u64>,
        ping_after: Option<f64>,
    ) -> PyResult<PoolOptions> {
        let options = PoolOptions {
            min_size,
            max_size,
            acquire_timeout: seconds("acquire_timeout", acquire_timeout)?,
            idle_timeout: optional_seconds("idle_timeout", idle_timeout)?,
            max_lifetime: optional_seconds("max_lifetime", max_lifetime)?,
            max_queries,
            ping_after: optional_seconds("ping_after", ping_after)?,
            connect_timeout,
            ..PoolOptions::default()
        };
        options.validate().map_err(|e| options_error(&e))?;
        Ok(options)
    }
}

#[pymethods]
impl Pool {
    // the argument list is fixed by the pool constructor in docs/design
    #[allow(clippy::too_many_arguments)]
    #[new]
    #[pyo3(signature = (
        options, *, min_size, max_size, acquire_timeout, idle_timeout, max_lifetime, max_queries,
        ping_after, reset_query
    ))]
    fn new(
        py: Python<'_>,
        options: &Bound<'_, PyAny>,
        min_size: usize,
        max_size: usize,
        acquire_timeout: f64,
        idle_timeout: Option<f64>,
        max_lifetime: Option<f64>,
        max_queries: Option<u64>,
        ping_after: Option<f64>,
        reset_query: Option<String>,
    ) -> PyResult<Self> {
        let connect = options_from_py(py, options)?;
        let settings = Self::options(
            connect.connect_timeout,
            min_size,
            max_size,
            acquire_timeout,
            idle_timeout,
            max_lifetime,
            max_queries,
            ping_after,
        )?;
        let handle = runtime::handle()?;
        let manager =
            PgManager::new(&connect, handle.clone(), reset_query).map_err(|e| to_py_err(py, &e))?;
        let pool = PgPool::new(manager, settings, handle).map_err(|e| options_error(&e))?;
        Ok(Self {
            inner: pool,
            statement_cache_size: connect.statement_cache_size,
            pid: std::process::id(),
        })
    }

    fn open<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        self.check_process(py)?;
        let pool = self.inner.clone();
        future_into_py(py, async move {
            pool.open().await.map_err(|e| PgError(e.into()))?;
            Ok(())
        })
    }

    #[pyo3(signature = (limit=None))]
    fn close<'py>(&self, py: Python<'py>, limit: Option<f64>) -> PyResult<Bound<'py, PyAny>> {
        let limit = optional_seconds("timeout", limit)?;
        let pool = self.inner.clone();
        future_into_py(py, async move {
            pool.close(limit).await;
            Ok(())
        })
    }

    fn terminate(&self) {
        self.inner.terminate();
    }

    fn expire_connections(&self) {
        self.inner.expire_connections();
    }

    #[pyo3(signature = (limit=None))]
    fn acquire<'py>(&self, py: Python<'py>, limit: Option<f64>) -> PyResult<Bound<'py, PyAny>> {
        self.check_process(py)?;
        let limit = optional_seconds("timeout", limit)?;
        let pool = self.inner.clone();
        let size = self.statement_cache_size;
        let cancelled = Arc::new(AtomicBool::new(false));
        let flag = Arc::clone(&cancelled);
        let future = future_into_py(py, async move {
            let acquiring = pool.acquire();
            let pooled = match limit {
                Some(limit) => tokio::time::timeout(limit, acquiring)
                    .await
                    .map_err(|_| PgError(Error::Core(CoreError::PoolTimeout)))?,
                None => acquiring.await,
            }
            .map_err(|e| PgError(e.into()))?;
            // dropping the checkout releases it, so a cancelled waiter leaves nothing behind
            if flag.load(std::sync::atomic::Ordering::Acquire) {
                return Err(PgError(Error::Core(CoreError::Closed)).into());
            }
            Ok(Connection::pooled(pooled, pool, size).map_err(PgError::from)?)
        })?;
        watch_flag(&future, cancelled)?;
        Ok(future)
    }

    // inline: the caller waits for the settle and the reset query
    #[pyo3(signature = (conn, reset_done=false))]
    fn release<'py>(
        &self,
        py: Python<'py>,
        conn: &Bound<'py, Connection>,
        reset_done: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.check_process(py)?;
        let pooled = conn
            .get()
            .take_pooled(reset_done)
            .map_err(|e| to_py_err(py, &e.into()))?;
        future_into_py(py, async move {
            pooled.release().await;
            Ok(())
        })
    }

    // off the caller's path: the lease is revoked now and the settle and reset run in the background
    #[pyo3(name = "_release_nowait")]
    fn release_nowait(&self, py: Python<'_>, conn: &Bound<'_, Connection>) -> PyResult<()> {
        self.check_process(py)?;
        let pooled = conn
            .get()
            .take_pooled(false)
            .map_err(|e| to_py_err(py, &e.into()))?;
        drop(pooled);
        Ok(())
    }

    // closes the connection instead of returning it, after a failed hook
    fn discard<'py>(
        &self,
        py: Python<'py>,
        conn: &Bound<'py, Connection>,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.check_process(py)?;
        let pooled = conn
            .get()
            .take_pooled(false)
            .map_err(|e| to_py_err(py, &e.into()))?;
        future_into_py(py, async move {
            pooled.discard().await;
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
        self.check_process(py)?;
        let params = crate::codec::extract(py, params)?;
        let args = FetchArgs {
            sql,
            mask,
            scalar,
            limit_one,
            arity,
        };
        let pool = self.inner.clone();
        let hook = Hook::default();
        let watched = hook.clone();
        let cancelled = Arc::new(AtomicBool::new(false));
        let flag = Arc::clone(&cancelled);
        let future = future_into_py(py, async move {
            let pooled = pool.acquire().await.map_err(|e| PgError(e.into()))?;
            // the Rust future is dropped late, so a cancelled waiter is caught here
            if flag.load(std::sync::atomic::Ordering::Acquire) {
                return Err(PgError(Error::Core(CoreError::Closed)).into());
            }
            let cancel = pooled.conn().map_err(PgError::from)?.cancel.clone();
            let access = Access {
                lease: Some(pooled.lease()),
                pool: Some(pool),
            };
            let result = exec::run_fetch(cancel, None, access, hook, params, args).await;
            // released in the background, after the rows are on their way to the caller
            drop(pooled);
            result
        })?;
        watch(&future, watched)?;
        watch_flag(&future, cancelled)?;
        Ok(future)
    }

    #[pyo3(name = "_execute")]
    fn execute<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        params: &Bound<'py, PyList>,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.check_process(py)?;
        let params = crate::codec::extract(py, params)?;
        let pool = self.inner.clone();
        let hook = Hook::default();
        let watched = hook.clone();
        let cancelled = Arc::new(AtomicBool::new(false));
        let flag = Arc::clone(&cancelled);
        let future = future_into_py(py, async move {
            let pooled = pool.acquire().await.map_err(|e| PgError(e.into()))?;
            if flag.load(std::sync::atomic::Ordering::Acquire) {
                return Err(PgError(Error::Core(CoreError::Closed)).into());
            }
            let cancel = pooled.conn().map_err(PgError::from)?.cancel.clone();
            let access = Access {
                lease: Some(pooled.lease()),
                pool: Some(pool),
            };
            let result = exec::run_execute(cancel, None, access, hook, params, sql).await;
            drop(pooled);
            result
        })?;
        watch(&future, watched)?;
        watch_flag(&future, cancelled)?;
        Ok(future)
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
        self.check_process(py)?;
        let args = StreamArgs {
            sql,
            mask,
            scalar,
            arity,
            batch_size,
        };
        RowStream::for_pool(py, self.inner.clone(), params, args)
    }

    #[getter]
    fn size(&self) -> usize {
        self.inner.size()
    }

    #[getter]
    fn idle(&self) -> usize {
        self.inner.idle()
    }

    #[getter]
    fn waiting(&self) -> usize {
        self.inner.waiting()
    }

    #[getter]
    fn min_size(&self) -> usize {
        self.inner.min_size()
    }

    #[getter]
    fn max_size(&self) -> usize {
        self.inner.max_size()
    }

    #[getter]
    fn closed(&self) -> bool {
        self.inner.closed()
    }
}

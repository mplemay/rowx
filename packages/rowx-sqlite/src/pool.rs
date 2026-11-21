use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use pyo3::prelude::*;
use pyo3::types::PyList;
use pyo3_async_runtimes::tokio::future_into_py;
use rowx_core::error::CoreError;
use rowx_py_core::exec::{watch, watch_flag};
use rowx_py_core::time::{optional_seconds, seconds};
use rowx_py_core::{errors as shared, runtime};
use rowx_sqlite::cancel::Hook;
use rowx_sqlite::error::Error;
use rowx_sqlite::pool::{Pool as SqlitePool, PoolOptions, SqliteManager, acquire};

use crate::conn::{Connection, Core, options_from_py};
use crate::errors::{Failure, SqliteError, to_py_err};
use crate::exec::{self, FetchArgs};
use crate::named::RowShape;
use crate::stream::{RowStream, StreamArgs};

// a pool failure as the caller sees it
fn pool_error(err: impl Into<Failure>) -> PyErr {
    SqliteError::from(err).into()
}

#[pyclass(frozen, module = "rowx_sqlite._core", name = "Pool")]
#[derive(Debug)]
pub struct Pool {
    inner: SqlitePool,
    statement_cache_size: usize,
    // the pool is unusable in a forked child, whose runtime threads are gone
    pid: u32,
}

impl Pool {
    fn check_process(&self, py: Python<'_>) -> PyResult<()> {
        shared::ensure_same_process(py, self.pid)
    }
}

#[pymethods]
impl Pool {
    // the parameters match the PoolImpl(...) call in packages/rowx-sqlite/src/rowx_sqlite/_pool.py
    // init, setup, and reset stay on the Python object
    #[allow(clippy::too_many_arguments)]
    #[new]
    #[pyo3(signature = (
        options, *, min_size, max_size, acquire_timeout, idle_timeout, max_lifetime, max_queries, ping_after
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
    ) -> PyResult<Self> {
        let connect = options_from_py(py, options)?;
        // the pool settings are validated in Python first; this is the backstop. The busy timeout bounds
        // a connect, so the pool needs no limit of its own.
        let settings = PoolOptions {
            min_size,
            max_size,
            acquire_timeout: seconds("acquire_timeout", acquire_timeout)?,
            idle_timeout: optional_seconds("idle_timeout", idle_timeout)?,
            max_lifetime: optional_seconds("max_lifetime", max_lifetime)?,
            max_queries,
            ping_after: optional_seconds("ping_after", ping_after)?,
            ..PoolOptions::default()
        };
        let statement_cache_size = connect.statement_cache;
        let pool = SqlitePool::new(SqliteManager::new(connect), settings, runtime::handle()?)
            .map_err(|e| shared::interface_error(py, &e.to_string()))?;
        Ok(Self {
            inner: pool,
            statement_cache_size,
            pid: std::process::id(),
        })
    }

    fn open<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        self.check_process(py)?;
        let pool = self.inner.clone();
        future_into_py(py, async move {
            pool.open().await.map_err(|e| pool_error(Error::from(e)))?;
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
            let acquiring = acquire(&pool);
            let pooled = match limit {
                Some(limit) => tokio::time::timeout(limit, acquiring)
                    .await
                    .map_err(|_| pool_error(CoreError::PoolTimeout))?,
                None => acquiring.await,
            }
            .map_err(pool_error)?;
            // dropping the checkout releases it, so a cancelled waiter leaves nothing behind
            if flag.load(Ordering::Acquire) {
                return Err(pool_error(CoreError::Closed));
            }
            Connection::pooled(pooled, &pool, size).map_err(pool_error)
        })?;
        watch_flag(&future, cancelled)?;
        Ok(future)
    }

    // inline: the caller waits for the settle and the reset
    #[pyo3(signature = (conn, reset_done=false))]
    fn release<'py>(
        &self,
        py: Python<'py>,
        conn: &Bound<'py, Connection>,
        // the reset below is cheap and idempotent, so a reset the caller already ran is not tracked
        reset_done: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let _ = reset_done;
        self.check_process(py)?;
        let pooled = conn
            .get()
            .take_pooled()
            .map_err(|e| to_py_err(py, &Failure::from(e)))?;
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
            .take_pooled()
            .map_err(|e| to_py_err(py, &Failure::from(e)))?;
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
            .take_pooled()
            .map_err(|e| to_py_err(py, &Failure::from(e)))?;
        future_into_py(py, async move {
            pooled.discard().await;
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
        self.check_process(py)?;
        let params = crate::codec::extract_all(py, params)?;
        let args = FetchArgs {
            sql,
            mask,
            scalar,
            limit_one,
            arity,
            shape,
        };
        let pool = self.inner.clone();
        let hook = Hook::default();
        let watched = hook.clone();
        let cancelled = Arc::new(AtomicBool::new(false));
        let flag = Arc::clone(&cancelled);
        let future = future_into_py(py, async move {
            let pooled = acquire(&pool).await.map_err(pool_error)?;
            // the Rust future is dropped late, so a cancelled waiter is caught here
            if flag.load(Ordering::Acquire) {
                return Err(pool_error(CoreError::Closed));
            }
            let core = Arc::new(Core::pooled(&pool, &pooled).map_err(pool_error)?);
            Python::attach(|py| core.check_params(py, params.len()))?;
            let result = exec::run_fetch(core, hook, params, args).await;
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
        let params = crate::codec::extract_all(py, params)?;
        let pool = self.inner.clone();
        let hook = Hook::default();
        let watched = hook.clone();
        let cancelled = Arc::new(AtomicBool::new(false));
        let flag = Arc::clone(&cancelled);
        let future = future_into_py(py, async move {
            let pooled = acquire(&pool).await.map_err(pool_error)?;
            if flag.load(Ordering::Acquire) {
                return Err(pool_error(CoreError::Closed));
            }
            let core = Arc::new(Core::pooled(&pool, &pooled).map_err(pool_error)?);
            Python::attach(|py| core.check_params(py, params.len()))?;
            let result = exec::run_execute(core, hook, params, sql).await;
            drop(pooled);
            result
        })?;
        watch(&future, watched)?;
        watch_flag(&future, cancelled)?;
        Ok(future)
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
        self.check_process(py)?;
        let args = StreamArgs {
            sql,
            mask,
            scalar,
            arity,
            batch_size,
            shape,
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

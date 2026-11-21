use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use pyo3::types::{PyList, PyTuple};
use pyo3_async_runtimes::tokio::future_into_py;
use rowx_core::error::CoreError;
use rowx_core::migrate::{Migration, PlanOptions, RevertTarget, SchemaName, TableName, resolve};
use rowx_py_core::exec::{watch, watch_flag};
use rowx_py_core::migrate::{Op, StopFirst, check_target, migration_rows, to_list};
use rowx_sqlite::cancel::{self, Hook};
use rowx_sqlite::conn::Connection as SqliteConnection;
use rowx_sqlite::migrate::{self as sqlite, MigrateConfig, MigrateError, MigrationDetail};
use rowx_sqlite::pool::{Pool as SqlitePool, acquire};

use crate::conn::{Connection, Core};
use crate::errors::{
    Failure, MigrateFailure, SqliteError, config_to_py_err, name_to_py_err, to_py_err,
};
use crate::exec::fail;
use crate::pool::Pool;

// everything one operation needs, owned so it can move into a task
#[derive(Debug, Clone)]
struct Job {
    migrations: Arc<Vec<Migration>>,
    config: MigrateConfig,
    ignore_missing: bool,
    allow_out_of_order: bool,
    op: Op,
    // set when the Python future is cancelled, so no step starts after that
    cancelled: Arc<AtomicBool>,
}

// What the call changed is reported with the state the migrations are in afterwards
async fn work(conn: &SqliteConnection, job: &Job) -> Result<Vec<MigrationDetail>, MigrateError> {
    let options = |target| PlanOptions {
        target,
        ignore_missing: job.ignore_missing,
        allow_out_of_order: job.allow_out_of_order,
    };
    let local = job.migrations.as_slice();
    let details = match job.op {
        Op::Info => sqlite::info(conn, &job.config, local).await?,
        Op::Run(target, false) => {
            sqlite::run(conn, &job.config, local, options(target), &job.cancelled).await?
        }
        Op::Run(target, true) => sqlite::run_dry(conn, &job.config, local, options(target)).await?,
        Op::Revert(target, false) => {
            sqlite::revert(
                conn,
                &job.config,
                local,
                target,
                job.ignore_missing,
                &job.cancelled,
            )
            .await?
        }
        Op::Revert(target, true) => {
            sqlite::revert_dry(conn, &job.config, local, target, job.ignore_missing).await?
        }
        Op::Skip(target, false) => {
            sqlite::skip(conn, &job.config, local, options(target), &job.cancelled).await?
        }
        Op::Skip(target, true) => {
            sqlite::skip_dry(conn, &job.config, local, options(target)).await?
        }
    };
    Ok(details)
}

// the connection's state follows the same rules as after any other statement
fn note(core: &Core, err: &MigrateError) {
    if let Some(error) = err.driver() {
        let _ = fail(&core.cancel.state, Failure::from(error.clone()));
    }
}

async fn run_job(core: Arc<Core>, hook: Hook, job: Job) -> PyResult<Py<PyAny>> {
    core.access
        .reserve(&core.cancel.state, &hook)
        .await
        .map_err(SqliteError::from)?;
    let task = Arc::clone(&core);
    let flag = Arc::clone(&job.cancelled);
    let migrations = Arc::clone(&job.migrations);
    let op = job.op;
    let pending = cancel::spawn(&core.cancel, &hook, async move {
        let conn = task.conn.lock().await;
        work(&conn, &job).await
    })
    .map_err(SqliteError::from)?;
    let result = StopFirst {
        flag,
        finished: false,
        inner: Box::pin(pending.wait()),
    }
    .await
    .map_err(SqliteError::from)?;
    match result {
        Ok(details) => Python::attach(|py| to_list(py, op, details)),
        Err(err) => {
            note(&core, &err);
            Err(MigrateFailure {
                error: err,
                migrations,
                reverting: matches!(op, Op::Revert(..)),
            }
            .into())
        }
    }
}

fn on_connection<'py>(py: Python<'py>, conn: &Connection, job: Job) -> PyResult<Bound<'py, PyAny>> {
    let flag = Arc::clone(&job.cancelled);
    // claimed synchronously so a second call made before the first is awaited fails immediately
    let claim = conn
        .core()
        .access
        .claim(&conn.core().cancel.state)
        .map_err(|e| to_py_err(py, &Failure::from(e)))?;
    let core = Arc::clone(conn.core());
    let hook = Hook::new(claim);
    let watched = hook.clone();
    let future = future_into_py(py, async move { run_job(core, hook, job).await })?;
    // the flag first: done callbacks run in order, so it is set before the hook sends the cancel
    watch_flag(&future, flag)?;
    watch(&future, watched)?;
    Ok(future)
}

// one acquire for the whole operation; the connection goes back through the pool's normal release
fn on_pool<'py>(py: Python<'py>, pool: &SqlitePool, job: Job) -> PyResult<Bound<'py, PyAny>> {
    let pool = pool.clone();
    let hook = Hook::default();
    let watched = hook.clone();
    let cancelled = Arc::clone(&job.cancelled);
    let flag = Arc::clone(&job.cancelled);
    let future = future_into_py(py, async move {
        let pooled = acquire(&pool).await.map_err(SqliteError::from)?;
        // a cancelled waiter must never run its migrations
        if flag.load(Ordering::Acquire) {
            return Err(SqliteError::from(CoreError::Closed).into());
        }
        let core = Arc::new(Core::pooled(&pool, &pooled).map_err(SqliteError::from)?);
        let result = run_job(core, hook, job).await;
        drop(pooled);
        result
    })?;
    watch_flag(&future, cancelled)?;
    watch(&future, watched)?;
    Ok(future)
}

#[pyclass(frozen, module = "rowx_sqlite._core", name = "Migrator")]
#[derive(Debug)]
pub struct Migrator {
    migrations: Arc<Vec<Migration>>,
    config: MigrateConfig,
    ignore_missing: bool,
    allow_out_of_order: bool,
}

impl Migrator {
    fn job(&self, op: Op) -> Job {
        Job {
            migrations: Arc::clone(&self.migrations),
            config: self.config.clone(),
            ignore_missing: self.ignore_missing,
            allow_out_of_order: self.allow_out_of_order,
            op,
            cancelled: Arc::new(AtomicBool::new(false)),
        }
    }

    fn start<'py>(
        &self,
        py: Python<'py>,
        executor: &Bound<'py, PyAny>,
        op: Op,
    ) -> PyResult<Bound<'py, PyAny>> {
        check_target(&self.migrations, op)?;
        let job = self.job(op);
        if let Ok(conn) = executor.cast::<Connection>() {
            return on_connection(py, conn.get(), job);
        }
        if let Ok(pool) = executor.cast::<Pool>() {
            return on_pool(py, pool.get().checked(py)?, job);
        }
        Err(PyTypeError::new_err(
            "a migrator runs on a Connection or a Pool",
        ))
    }
}

#[pymethods]
impl Migrator {
    #[new]
    #[pyo3(signature = (path, table, create_schemas, ignore_missing, allow_out_of_order))]
    fn new(
        py: Python<'_>,
        path: &Bound<'_, PyAny>,
        table: &str,
        create_schemas: Vec<String>,
        ignore_missing: bool,
        allow_out_of_order: bool,
    ) -> PyResult<Self> {
        let path: PathBuf = path.extract()?;
        // a bad name, a schema SQLite lacks or a schema to create is a ValueError before any IO
        let table = TableName::parse(table).map_err(|e| name_to_py_err(&e))?;
        let create_schemas = create_schemas
            .into_iter()
            .map(|schema| SchemaName::parse(&schema))
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| name_to_py_err(&e))?;
        let config =
            MigrateConfig::new(table, &create_schemas).map_err(|e| config_to_py_err(&e))?;
        // reading and checksumming the files is IO, so it runs without the GIL
        let migrations = py
            .detach(|| resolve(&path))
            .map_err(|e| rowx_py_core::errors::source_error(py, &e))?;
        Ok(Self {
            migrations: Arc::new(migrations),
            config,
            ignore_missing,
            allow_out_of_order,
        })
    }

    // (version, description, kind, path, down_path, checksum, down_checksum, no_transaction), one per
    // version: a reversible pair is a single entry
    #[getter]
    fn migrations<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        migration_rows(py, &self.migrations)
    }

    // the parsed history table, `(name,)` or `("main", name)`, each part as the catalog stores it
    #[getter]
    fn table<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyTuple>> {
        let table = self.config.table();
        let parts: Vec<&str> = table.schema().into_iter().chain([table.name()]).collect();
        PyTuple::new(py, parts)
    }

    #[pyo3(signature = (executor, target=None, dry_run=false))]
    fn run<'py>(
        &self,
        py: Python<'py>,
        executor: &Bound<'py, PyAny>,
        target: Option<i64>,
        dry_run: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.start(py, executor, Op::Run(target, dry_run))
    }

    // `revert_all` reverts everything, otherwise `None` is the latest applied version only and
    // `Some(V)` every applied version above `V`
    #[pyo3(signature = (executor, target=None, revert_all=false, dry_run=false))]
    fn revert<'py>(
        &self,
        py: Python<'py>,
        executor: &Bound<'py, PyAny>,
        target: Option<i64>,
        revert_all: bool,
        dry_run: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let target = match (revert_all, target) {
            (true, _) => RevertTarget::All,
            (false, None) => RevertTarget::Latest,
            (false, Some(version)) => RevertTarget::Above(version),
        };
        self.start(py, executor, Op::Revert(target, dry_run))
    }

    #[pyo3(signature = (executor, target=None, dry_run=false))]
    fn skip<'py>(
        &self,
        py: Python<'py>,
        executor: &Bound<'py, PyAny>,
        target: Option<i64>,
        dry_run: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.start(py, executor, Op::Skip(target, dry_run))
    }

    fn info<'py>(
        &self,
        py: Python<'py>,
        executor: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.start(py, executor, Op::Info)
    }
}

use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use pyo3::types::{PyList, PyTuple};
use pyo3_async_runtimes::tokio::future_into_py;
use rowx_core::error::CoreError;
use rowx_core::migrate::{
    Migration, MigrationKind, MigrationStatus, PlanOptions, RevertTarget, SchemaName, TableName,
    resolve,
};
use rowx_core::state::Claim;
use rowx_postgres::cancel::{self, Cancel, Hook};
use rowx_postgres::conn::Connection as PgConnection;
use rowx_postgres::error::Error;
use rowx_postgres::migrate::{self as pg, MigrateConfig, MigrateError, MigrationDetail};
use rowx_postgres::pool::Pool as PgPool;

use crate::conn::Connection;
use crate::errors::{MigrateFailure, PgError, name_to_py_err, to_py_err};
use crate::exec::{Access, watch, watch_flag};
use crate::pool::Pool;

#[derive(Debug, Clone, Copy)]
enum Op {
    // the target, and whether it is a dry run
    Run(Option<i64>, bool),
    Undo(RevertTarget, bool),
    Skip(Option<i64>),
    Info,
}

// everything one operation needs, owned so it can move into a task
#[derive(Debug, Clone)]
struct Job {
    migrations: Arc<Vec<Migration>>,
    config: MigrateConfig,
    ignore_missing: bool,
    allow_out_of_order: bool,
    op: Op,
}

// a row of the result: version, description, state, installed_on (microseconds since the epoch), execution_time (ns)
#[derive(Debug)]
struct Status {
    version: i64,
    description: String,
    state: &'static str,
    installed_on_micros: Option<i64>,
    execution_time: Option<i64>,
}

const fn state_name(status: MigrationStatus) -> &'static str {
    match status {
        MigrationStatus::Applied => "applied",
        MigrationStatus::Pending => "pending",
        MigrationStatus::Modified => "modified",
        MigrationStatus::Missing => "missing",
        MigrationStatus::Dirty => "dirty",
    }
}

const fn kind_name(kind: MigrationKind) -> &'static str {
    match kind {
        MigrationKind::Simple => "simple",
        MigrationKind::Up => "up",
        MigrationKind::Down => "down",
    }
}

fn status(detail: MigrationDetail) -> Status {
    Status {
        version: detail.info.version,
        description: detail.info.description.unwrap_or_default(),
        state: state_name(detail.info.status),
        installed_on_micros: detail.installed_on_micros,
        execution_time: detail.execution_time,
    }
}

// What the call changed is reported with the state the migrations are in afterwards, which the
// executor reads under the same lock as the change
async fn work(conn: &PgConnection, job: &Job) -> Result<Vec<Status>, MigrateError> {
    let options = |target| PlanOptions {
        target,
        ignore_missing: job.ignore_missing,
        allow_out_of_order: job.allow_out_of_order,
    };
    let local = job.migrations.as_slice();
    let details = match job.op {
        Op::Info => pg::info(conn, &job.config, local).await?,
        Op::Run(target, false) => pg::run(conn, &job.config, local, options(target)).await?,
        Op::Run(target, true) => pg::run_dry(conn, &job.config, local, options(target)).await?,
        Op::Undo(target, false) => {
            pg::undo(conn, &job.config, local, target, job.ignore_missing).await?
        }
        Op::Undo(target, true) => {
            pg::undo_dry(conn, &job.config, local, target, job.ignore_missing).await?
        }
        Op::Skip(target) => pg::skip(conn, &job.config, local, options(target)).await?,
    };
    Ok(details.into_iter().map(status).collect())
}

// the lock and the connection's state follow the same rules as any other statement
fn note(access: &Access, cancel: &Cancel, err: &MigrateError) {
    if let MigrateError::Connection(e) | MigrateError::Failed { source: e, .. } = err {
        let _ = access.fail(&cancel.state, e.clone());
    }
}

// An error before the plan's first step changes nothing; any later one may follow committed
// migrations or a file that changed the schema before it failed, so it drops the plans as well
fn may_have_changed(op: Op, result: &Result<Vec<Status>, MigrateError>) -> bool {
    match (op, result) {
        (Op::Info | Op::Run(_, true) | Op::Undo(_, true), _)
        | (_, Err(MigrateError::Plan(_) | MigrateError::Source(_))) => false,
        (_, Ok(statuses)) => !statuses.is_empty(),
        (_, Err(MigrateError::Failed { .. } | MigrateError::Connection(_))) => true,
    }
}

async fn run_job(
    cancel: Cancel,
    claim: Option<Claim>,
    access: Access,
    hook: Hook,
    job: Job,
) -> PyResult<Py<PyAny>> {
    let claim = access.settle_claim(&cancel.state, claim).await?;
    let inner = Arc::clone(&cancel.backend.conn);
    let pool = access.pool.clone();
    // the invalidation runs in the task, so it also happens when the caller is cancelled
    let pending = cancel::spawn(&cancel, claim, &hook, async move {
        let mut conn = inner.lock().await;
        let result = work(&conn, &job).await;
        if may_have_changed(job.op, &result) {
            // the connection's own prepared statements may now be stale
            if let Ok((_, cache)) = conn.parts() {
                cache.clear();
            }
            if let Some(pool) = &pool {
                pool.bump_epoch();
            }
        }
        drop(conn);
        result
    });
    let result = pending.wait().await.map_err(|e| PgError(e.into()))?;
    match result {
        Ok(statuses) => Python::attach(|py| to_list(py, statuses)),
        Err(err) => {
            note(&access, &cancel, &err);
            Err(MigrateFailure(err).into())
        }
    }
}

fn to_list(py: Python<'_>, statuses: Vec<Status>) -> PyResult<Py<PyAny>> {
    let rows = statuses
        .into_iter()
        .map(|s| {
            PyTuple::new(
                py,
                [
                    s.version.into_pyobject(py)?.into_any(),
                    s.description.into_pyobject(py)?.into_any(),
                    s.state.into_pyobject(py)?.into_any(),
                    s.installed_on_micros.into_pyobject(py)?.into_any(),
                    s.execution_time.into_pyobject(py)?.into_any(),
                ],
            )
        })
        .collect::<PyResult<Vec<_>>>()?;
    Ok(PyList::new(py, rows)?.into_any().unbind())
}

fn on_connection<'py>(py: Python<'py>, conn: &Connection, job: Job) -> PyResult<Bound<'py, PyAny>> {
    // claimed synchronously so a second call made before the first is awaited fails immediately
    let claim = conn
        .access
        .claim(&conn.cancel.state)
        .map_err(|e| to_py_err(py, &e.into()))?;
    let cancel = conn.cancel.clone();
    let access = conn.access.clone();
    let hook = Hook::default();
    let watched = hook.clone();
    let future = future_into_py(py, async move {
        run_job(cancel, claim, access, hook, job).await
    })?;
    watch(&future, watched)?;
    Ok(future)
}

// one acquire for the whole operation; the connection goes back through the pool's normal release
fn on_pool<'py>(py: Python<'py>, pool: &PgPool, job: Job) -> PyResult<Bound<'py, PyAny>> {
    let pool = pool.clone();
    let hook = Hook::default();
    let watched = hook.clone();
    let cancelled = Arc::new(AtomicBool::new(false));
    let flag = Arc::clone(&cancelled);
    let future = future_into_py(py, async move {
        let pooled = pool.acquire().await.map_err(|e| PgError(e.into()))?;
        // a cancelled waiter must never run its migrations
        if flag.load(Ordering::Acquire) {
            return Err(PgError(Error::Core(CoreError::Closed)).into());
        }
        let cancel = pooled.conn().map_err(PgError::from)?.cancel.clone();
        let access = Access {
            lease: Some(pooled.lease()),
            pool: Some(pool),
        };
        let result = run_job(cancel, None, access, hook, job).await;
        drop(pooled);
        result
    })?;
    watch(&future, watched)?;
    watch_flag(&future, cancelled)?;
    Ok(future)
}

#[pyclass(frozen, module = "rowx._core.postgres", name = "Migrator")]
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
        }
    }

    fn start<'py>(
        &self,
        py: Python<'py>,
        executor: &Bound<'py, PyAny>,
        op: Op,
    ) -> PyResult<Bound<'py, PyAny>> {
        let job = self.job(op);
        if let Ok(conn) = executor.cast::<Connection>() {
            return on_connection(py, conn.get(), job);
        }
        if let Ok(pool) = executor.cast::<Pool>() {
            return on_pool(py, pool.get().inner_checked(py)?, job);
        }
        Err(PyTypeError::new_err(
            "a migrator runs on a Connection or a Pool",
        ))
    }
}

#[pymethods]
impl Migrator {
    #[new]
    #[pyo3(signature = (path, table, create_schemas, locking, ignore_missing, allow_out_of_order))]
    fn new(
        py: Python<'_>,
        path: &Bound<'_, PyAny>,
        table: &str,
        create_schemas: Vec<String>,
        locking: bool,
        ignore_missing: bool,
        allow_out_of_order: bool,
    ) -> PyResult<Self> {
        let path: PathBuf = path.extract()?;
        // a bad name is a ValueError before any IO
        let table = TableName::parse(table).map_err(|e| name_to_py_err(&e))?;
        let create_schemas = create_schemas
            .into_iter()
            .map(|schema| SchemaName::parse(&schema))
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| name_to_py_err(&e))?;
        // reading and checksumming the files is IO, so it runs without the GIL
        let migrations = py
            .detach(|| resolve(&path))
            .map_err(|e| rowx_py_core::errors::source_error(py, &e))?;
        Ok(Self {
            migrations: Arc::new(migrations),
            config: MigrateConfig {
                table,
                create_schemas,
                locking,
            },
            ignore_missing,
            allow_out_of_order,
        })
    }

    // (version, description, kind, checksum, no_transaction, path)
    #[getter]
    fn migrations<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let rows = self
            .migrations
            .iter()
            .map(|m| {
                PyTuple::new(
                    py,
                    [
                        m.version.into_pyobject(py)?.into_any(),
                        m.description.as_str().into_pyobject(py)?.into_any(),
                        kind_name(m.kind).into_pyobject(py)?.into_any(),
                        pyo3::types::PyBytes::new(py, &m.checksum).into_any(),
                        pyo3::types::PyBool::new(py, m.no_transaction)
                            .to_owned()
                            .into_any(),
                        m.path.as_path().into_pyobject(py)?.into_any(),
                    ],
                )
            })
            .collect::<PyResult<Vec<_>>>()?;
        PyList::new(py, rows)
    }

    // the parsed history table, `(name,)` or `(schema, name)`, each part as the server stores it
    // (folded or unquoted), so a caller can quote it without parsing the configured string again
    #[getter]
    fn table<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyTuple>> {
        let table = &self.config.table;
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
    #[pyo3(signature = (executor, target, revert_all=false, dry_run=false))]
    fn undo<'py>(
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
        self.start(py, executor, Op::Undo(target, dry_run))
    }

    #[pyo3(signature = (executor, target=None))]
    fn skip<'py>(
        &self,
        py: Python<'py>,
        executor: &Bound<'py, PyAny>,
        target: Option<i64>,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.start(py, executor, Op::Skip(target))
    }

    fn info<'py>(
        &self,
        py: Python<'py>,
        executor: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.start(py, executor, Op::Info)
    }
}

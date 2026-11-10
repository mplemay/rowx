use pyo3::prelude::*;
use pyo3_async_runtimes::tokio::future_into_py;
use rowx_postgres::admin as pg;
use rowx_postgres::dsn::ConnectOptions;
use rowx_py_core::runtime;

use crate::conn::options_from_py;
use crate::errors::{PgError, interface_error, to_py_err};

#[pyclass(
    frozen,
    eq,
    hash,
    get_all,
    skip_from_py_object,
    module = "rowx_postgres._core",
    name = "DatabaseInfo"
)]
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct DatabaseInfo {
    name: String,
    comment: Option<String>,
    // sessions connected right now (`pg_stat_database.numbackends`, an int8)
    sessions: i64,
}

impl From<pg::DatabaseInfo> for DatabaseInfo {
    fn from(info: pg::DatabaseInfo) -> Self {
        Self {
            name: info.name,
            comment: info.comment,
            sessions: info.sessions,
        }
    }
}

#[pymethods]
impl DatabaseInfo {
    #[new]
    #[pyo3(signature = (*, name, comment, sessions))]
    const fn new(name: String, comment: Option<String>, sessions: i64) -> Self {
        Self {
            name,
            comment,
            sessions,
        }
    }
}

// accepts a DSN or a `rowx_postgres.ConnectOptions`; the name defaults to the options' own database
fn resolve(
    py: Python<'_>,
    options: &Bound<'_, PyAny>,
    name: Option<String>,
) -> PyResult<(ConnectOptions, String)> {
    let options = match options.extract::<String>() {
        Ok(dsn) => ConnectOptions::from_dsn(&dsn).map_err(|e| to_py_err(py, &e))?,
        Err(_) => options_from_py(py, options)?,
    };
    let name = name.or_else(|| options.database.clone()).ok_or_else(|| {
        interface_error(
            py,
            "no database name: pass `name` or put one in the options",
        )
    })?;
    Ok((options, name))
}

/// # Errors
/// `InterfaceError` for bad options or name; connection and server failures surface when awaited.
#[pyfunction]
#[pyo3(signature = (target, name=None))]
pub fn database_exists<'py>(
    py: Python<'py>,
    target: &Bound<'py, PyAny>,
    name: Option<String>,
) -> PyResult<Bound<'py, PyAny>> {
    let (options, name) = resolve(py, target, name)?;
    let handle = runtime::handle()?;
    future_into_py(py, async move {
        Ok(pg::database_exists(&options, &name, &handle)
            .await
            .map_err(PgError)?)
    })
}

/// # Errors
/// As `database_exists`; `DatabaseError` when the database exists already.
#[pyfunction]
#[pyo3(signature = (target, name=None, *, template=None, comment=None))]
pub fn create_database<'py>(
    py: Python<'py>,
    target: &Bound<'py, PyAny>,
    name: Option<String>,
    template: Option<String>,
    comment: Option<String>,
) -> PyResult<Bound<'py, PyAny>> {
    let (options, name) = resolve(py, target, name)?;
    let handle = runtime::handle()?;
    future_into_py(py, async move {
        pg::create_database(
            &options,
            &name,
            template.as_deref(),
            comment.as_deref(),
            &handle,
        )
        .await
        .map_err(PgError)?;
        Ok(())
    })
}

/// # Errors
/// As `database_exists`; a missing database is not an error.
#[pyfunction]
#[pyo3(signature = (target, name=None, *, force=false))]
pub fn drop_database<'py>(
    py: Python<'py>,
    target: &Bound<'py, PyAny>,
    name: Option<String>,
    force: bool,
) -> PyResult<Bound<'py, PyAny>> {
    let (options, name) = resolve(py, target, name)?;
    let handle = runtime::handle()?;
    future_into_py(py, async move {
        pg::drop_database(&options, &name, force, &handle)
            .await
            .map_err(PgError)?;
        Ok(())
    })
}

/// # Errors
/// As `database_exists`. Each item is a `DatabaseInfo`.
#[pyfunction]
#[pyo3(signature = (target, prefix))]
pub fn list_databases<'py>(
    py: Python<'py>,
    target: &Bound<'py, PyAny>,
    prefix: String,
) -> PyResult<Bound<'py, PyAny>> {
    let (options, _) = resolve(py, target, Some(String::new()))?;
    let handle = runtime::handle()?;
    future_into_py(py, async move {
        let found = pg::list_databases(&options, &prefix, &handle)
            .await
            .map_err(PgError)?;
        Ok(found
            .into_iter()
            .map(DatabaseInfo::from)
            .collect::<Vec<DatabaseInfo>>())
    })
}

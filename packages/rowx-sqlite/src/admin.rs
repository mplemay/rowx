use pyo3::prelude::*;
use pyo3_async_runtimes::tokio::future_into_py;
use rowx_sqlite::admin as sqlite;

use crate::errors::AdminFailure;

#[pyclass(
    frozen,
    eq,
    hash,
    get_all,
    skip_from_py_object,
    module = "rowx_sqlite._core",
    name = "DatabaseInfo"
)]
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct DatabaseInfo {
    name: String,
    comment: Option<String>,
    sessions: i64,
}

impl From<sqlite::DatabaseInfo> for DatabaseInfo {
    fn from(info: sqlite::DatabaseInfo) -> Self {
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

// The work runs on a thread of its own (see `rowx_sqlite::admin`), so the GIL is not held while it waits.

/// # Errors
/// `InterfaceError` for a bad name; `DatabaseError` for a path that cannot be inspected.
#[pyfunction]
pub fn database_exists(py: Python<'_>, name: String) -> PyResult<Bound<'_, PyAny>> {
    future_into_py(py, async move {
        Ok(sqlite::exists(&name).await.map_err(AdminFailure)?)
    })
}

/// # Errors
/// As `database_exists`; `DatabaseError` (42P04) when the name is taken.
#[pyfunction]
#[pyo3(signature = (name, *, template=None))]
pub fn create_database(
    py: Python<'_>,
    name: String,
    template: Option<String>,
) -> PyResult<Bound<'_, PyAny>> {
    future_into_py(py, async move {
        sqlite::create(&name, template.as_deref())
            .await
            .map_err(AdminFailure)?;
        Ok(())
    })
}

/// `force` skips the check that no other connection holds the database open (outside Windows, where the
/// operating system refuses on its own).
///
/// # Errors
/// As `database_exists`; a missing file is not an error.
#[pyfunction]
#[pyo3(signature = (name, *, force=false))]
pub fn drop_database(py: Python<'_>, name: String, force: bool) -> PyResult<Bound<'_, PyAny>> {
    future_into_py(py, async move {
        sqlite::drop_database(&name, force)
            .await
            .map_err(AdminFailure)?;
        Ok(())
    })
}

/// # Errors
/// As `database_exists`. Each item is a `DatabaseInfo`.
#[pyfunction]
pub fn list_databases(
    py: Python<'_>,
    anchor: String,
    prefix: String,
) -> PyResult<Bound<'_, PyAny>> {
    future_into_py(py, async move {
        let found = sqlite::list(&anchor, &prefix).await.map_err(AdminFailure)?;
        Ok(found
            .into_iter()
            .map(DatabaseInfo::from)
            .collect::<Vec<DatabaseInfo>>())
    })
}

/// # Errors
/// As `database_exists`; `DatabaseError` when the directory is missing or the file is not a database.
#[pyfunction]
pub fn database_ready(py: Python<'_>, name: String, seconds: f64) -> PyResult<Bound<'_, PyAny>> {
    future_into_py(py, async move {
        sqlite::ready(&name, seconds).await.map_err(AdminFailure)?;
        Ok(())
    })
}

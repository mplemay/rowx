use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::PyType;
use rowx_core::error::CoreError;
use rowx_core::migrate::{PlanError, SourceError};

static INTERFACE_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static DATABASE_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static POOL_TIMEOUT_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static BUSY_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static ROW_SHAPE_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static DECODE_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static ENCODE_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static MIGRATION_SOURCE_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static MIGRATION_CHECKSUM_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static MIGRATION_MISSING_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static MIGRATION_ORDER_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static DIRTY_MIGRATION_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static MIGRATION_FAILED_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();

fn class<'py>(
    py: Python<'py>,
    cell: &'static PyOnceLock<Py<PyType>>,
    name: &str,
) -> PyResult<&'py Bound<'py, PyType>> {
    let cls = cell.get_or_try_init(py, || {
        Ok::<_, PyErr>(
            py.import("rowx.errors")?
                .getattr(name)?
                .cast_into::<PyType>()?
                .unbind(),
        )
    })?;
    Ok(cls.bind(py))
}

/// Imports the `rowx.errors` classes so a missing or renamed class fails at module init.
///
/// # Errors
/// An import or attribute error when `rowx.errors` lacks a class.
pub fn init(py: Python<'_>) -> PyResult<()> {
    class(py, &INTERFACE_ERROR, "InterfaceError")?;
    class(py, &DATABASE_ERROR, "DatabaseError")?;
    class(py, &BUSY_ERROR, "ConnectionBusyError")?;
    class(py, &POOL_TIMEOUT_ERROR, "PoolTimeoutError")?;
    class(py, &ROW_SHAPE_ERROR, "RowShapeError")?;
    class(py, &DECODE_ERROR, "DecodeError")?;
    class(py, &ENCODE_ERROR, "EncodeError")?;
    class(py, &MIGRATION_SOURCE_ERROR, "MigrationSourceError")?;
    class(py, &MIGRATION_CHECKSUM_ERROR, "MigrationChecksumError")?;
    class(py, &MIGRATION_MISSING_ERROR, "MigrationMissingError")?;
    class(py, &MIGRATION_ORDER_ERROR, "MigrationOrderError")?;
    class(py, &DIRTY_MIGRATION_ERROR, "DirtyMigrationError")?;
    class(py, &MIGRATION_FAILED_ERROR, "MigrationFailedError")?;
    Ok(())
}

fn build<'py, A>(
    py: Python<'py>,
    cell: &'static PyOnceLock<Py<PyType>>,
    name: &str,
    args: A,
) -> PyErr
where
    A: pyo3::call::PyCallArgs<'py>,
{
    let made = class(py, cell, name).and_then(|cls| cls.call1(args));
    match made {
        Ok(instance) => PyErr::from_value(instance),
        Err(e) => e,
    }
}

#[must_use]
pub fn interface_error(py: Python<'_>, message: &str) -> PyErr {
    build(py, &INTERFACE_ERROR, "InterfaceError", (message,))
}

#[must_use]
pub fn busy_error(py: Python<'_>, message: &str) -> PyErr {
    build(py, &BUSY_ERROR, "ConnectionBusyError", (message,))
}

#[must_use]
pub fn pool_timeout_error(py: Python<'_>, message: &str) -> PyErr {
    build(py, &POOL_TIMEOUT_ERROR, "PoolTimeoutError", (message,))
}

#[must_use]
pub fn row_shape_error(py: Python<'_>, message: &str) -> PyErr {
    build(py, &ROW_SHAPE_ERROR, "RowShapeError", (message,))
}

#[must_use]
pub fn decode_error(py: Python<'_>, message: &str) -> PyErr {
    build(py, &DECODE_ERROR, "DecodeError", (message,))
}

#[must_use]
pub fn encode_error(py: Python<'_>, message: &str) -> PyErr {
    build(py, &ENCODE_ERROR, "EncodeError", (message,))
}

#[must_use]
pub fn database_error(
    py: Python<'_>,
    sqlstate: &str,
    message: &str,
    detail: Option<&str>,
    hint: Option<&str>,
    position: Option<u32>,
) -> PyErr {
    build(
        py,
        &DATABASE_ERROR,
        "DatabaseError",
        (sqlstate, message, detail, hint, position),
    )
}

// the variants every backend shares map identically
#[must_use]
pub fn core_to_py_err(py: Python<'_>, err: CoreError) -> PyErr {
    match err {
        CoreError::Busy => busy_error(py, &err.to_string()),
        CoreError::PoolTimeout => pool_timeout_error(py, &err.to_string()),
        CoreError::Closed
        | CoreError::Broken
        | CoreError::PoolClosed
        | CoreError::StaleLease
        | CoreError::Forked => interface_error(py, &err.to_string()),
    }
}

/// A migration file that cannot be resolved; `version` is set when the error is about one version.
#[must_use]
pub fn source_error(py: Python<'_>, err: &SourceError) -> PyErr {
    build(
        py,
        &MIGRATION_SOURCE_ERROR,
        "MigrationSourceError",
        (err.to_string(), err.version()),
    )
}

/// A failed file or commit; the caller sets the `DatabaseError` as the cause.
#[must_use]
pub fn migration_failed_error(py: Python<'_>, version: i64, message: &str) -> PyErr {
    build(
        py,
        &MIGRATION_FAILED_ERROR,
        "MigrationFailedError",
        (message, version),
    )
}

// a bad target is an ordering problem, and a missing `.down.sql` is a problem with the files
#[must_use]
pub fn plan_error(py: Python<'_>, err: &PlanError) -> PyErr {
    let args = (err.to_string(), err.version());
    match err {
        PlanError::Dirty { .. } => build(py, &DIRTY_MIGRATION_ERROR, "DirtyMigrationError", args),
        PlanError::Missing { .. } => {
            build(py, &MIGRATION_MISSING_ERROR, "MigrationMissingError", args)
        }
        PlanError::Checksum { .. } => build(
            py,
            &MIGRATION_CHECKSUM_ERROR,
            "MigrationChecksumError",
            args,
        ),
        PlanError::Order { .. } | PlanError::TargetNotFound { .. } => {
            build(py, &MIGRATION_ORDER_ERROR, "MigrationOrderError", args)
        }
        PlanError::MissingDown { .. } => {
            build(py, &MIGRATION_SOURCE_ERROR, "MigrationSourceError", args)
        }
    }
}

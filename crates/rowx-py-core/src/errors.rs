use std::path::Path;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBytes, PyTuple, PyType};
use rowx_core::error::CoreError;
use rowx_core::migrate::{Migration, MigrationDetail, PlanError, SourceError};
use rowx_core::row_shape::ShapeErrors;

use crate::shape::{RowShapeData, render_null, render_shape};

static INTERFACE_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static DATABASE_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static POOL_TIMEOUT_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static BUSY_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static ROW_SHAPE_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static DECODE_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static ROW_CONSTRUCT_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static ENCODE_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static MIGRATION_SOURCE_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static IRREVERSIBLE_MIGRATION_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();
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
            py.import("rowx_core.errors")?
                .getattr(name)?
                .cast_into::<PyType>()?
                .unbind(),
        )
    })?;
    Ok(cls.bind(py))
}

/// Imports the `rowx_core.errors` classes so a missing or renamed class fails at module init.
///
/// # Errors
/// An import or attribute error when `rowx_core.errors` lacks a class.
pub fn init(py: Python<'_>) -> PyResult<()> {
    class(py, &INTERFACE_ERROR, "InterfaceError")?;
    class(py, &DATABASE_ERROR, "DatabaseError")?;
    class(py, &BUSY_ERROR, "ConnectionBusyError")?;
    class(py, &POOL_TIMEOUT_ERROR, "PoolTimeoutError")?;
    class(py, &ROW_SHAPE_ERROR, "RowShapeError")?;
    class(py, &DECODE_ERROR, "DecodeError")?;
    class(py, &ROW_CONSTRUCT_ERROR, "RowConstructError")?;
    class(py, &ENCODE_ERROR, "EncodeError")?;
    class(py, &MIGRATION_SOURCE_ERROR, "MigrationSourceError")?;
    class(
        py,
        &IRREVERSIBLE_MIGRATION_ERROR,
        "IrreversibleMigrationError",
    )?;
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

/// A pool timeout whose `__cause__` is the connect error that kept the pool from connecting.
#[must_use]
pub fn pool_timeout_error_with_cause(py: Python<'_>, message: &str, cause: PyErr) -> PyErr {
    let err = pool_timeout_error(py, message);
    err.set_cause(py, Some(cause));
    err
}

#[must_use]
pub fn row_shape_error(py: Python<'_>, message: &str) -> PyErr {
    build(py, &ROW_SHAPE_ERROR, "RowShapeError", (message,))
}

/// The `RowShapeError` for a named row whose columns do not map onto its fields.
#[must_use]
pub fn named_shape_error(
    py: Python<'_>,
    data: &RowShapeData,
    errors: &ShapeErrors,
    columns: &[String],
) -> PyErr {
    row_shape_error(
        py,
        &render_shape(data.type_name(), data.key_names(), errors, columns),
    )
}

/// The `RowShapeError` for a NULL in a mapped field that is not nullable.
#[must_use]
pub fn named_null_error(
    py: Python<'_>,
    data: &RowShapeData,
    field: usize,
    column: usize,
    name: &str,
    row: usize,
) -> PyErr {
    let key = data.key_names().get(field).map_or("", String::as_str);
    row_shape_error(py, &render_null(data.type_name(), key, column, name, row))
}

#[must_use]
pub fn decode_error(py: Python<'_>, message: &str) -> PyErr {
    build(py, &DECODE_ERROR, "DecodeError", (message,))
}

/// A named row's constructor or validator raised `cause`, which becomes the `__cause__`.
#[must_use]
pub fn row_construct_error(
    py: Python<'_>,
    message: &str,
    type_name: &str,
    row_index: usize,
    cause: PyErr,
) -> PyErr {
    let err = build(
        py,
        &ROW_CONSTRUCT_ERROR,
        "RowConstructError",
        (message, type_name, row_index),
    );
    err.set_cause(py, Some(cause));
    err
}

/// A named row whose constructor or validator raised `cause`, an `Exception`.
#[must_use]
pub fn row_construct_failed(
    py: Python<'_>,
    cause: PyErr,
    type_name: &str,
    row_index: usize,
) -> PyErr {
    let detail = cause.value(py).repr().map_or_else(
        |_| "<unrepresentable>".to_owned(),
        |r| r.to_string_lossy().into_owned(),
    );
    row_construct_error(
        py,
        &format!("{type_name} row {row_index}: {detail}"),
        type_name,
        row_index,
        cause,
    )
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
    code: Option<&str>,
) -> PyErr {
    build(
        py,
        &DATABASE_ERROR,
        "DatabaseError",
        (sqlstate, message, detail, hint, position, code),
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

/// # Errors
/// `InterfaceError` when the current process is not the one (`pid`) that created the pool.
pub fn ensure_same_process(py: Python<'_>, pid: u32) -> PyResult<()> {
    if std::process::id() == pid {
        Ok(())
    } else {
        Err(core_to_py_err(py, CoreError::Forked))
    }
}

/// A migration file that cannot be resolved; `version` is set when the error is about one version.
#[must_use]
pub fn source_error(py: Python<'_>, err: &SourceError) -> PyErr {
    build(
        py,
        &MIGRATION_SOURCE_ERROR,
        "MigrationSourceError",
        (err.to_string(), err.path(), err.version()),
    )
}

/// A failed file or commit; the caller sets the `DatabaseError` as the cause.
///
/// `applied` holds a `(version, nanoseconds or None)` pair for each migration completed before the
/// failure, which the Python wrapper turns into results.
///
/// # Errors
/// A Python error when the arguments cannot be converted.
pub fn migration_failed_error<'py>(
    py: Python<'py>,
    message: &str,
    version: i64,
    path: Option<&Path>,
    dirty: bool,
    applied: &[(i64, Option<i64>)],
    error: Bound<'py, PyAny>,
) -> PyResult<PyErr> {
    let applied = PyTuple::new(py, applied)?;
    Ok(build(
        py,
        &MIGRATION_FAILED_ERROR,
        "MigrationFailedError",
        (message, version, path, dirty, applied, error),
    ))
}

/// What a migration file's failure is reported with, besides the backend's own error.
#[derive(Debug, Clone, Copy)]
pub struct FileFailure<'a> {
    pub message: &'a str,
    pub version: i64,
    // the history row was left with `success = false`
    pub dirty: bool,
    // the status of each migration applied before this one failed
    pub applied: &'a [MigrationDetail],
    pub migrations: &'a [Migration],
    // a revert runs the `.down.sql` file, everything else the forward one
    pub reverting: bool,
}

/// The backend-neutral failure of a migration file: `cause` is the backend's `DatabaseError` for it,
/// which becomes the `MigrationFailedError`'s cause.
#[must_use]
pub fn migration_file_failed(py: Python<'_>, failure: &FileFailure<'_>, cause: PyErr) -> PyErr {
    let path = failure
        .migrations
        .iter()
        .find(|m| m.version == failure.version && m.kind.is_down() == failure.reverting)
        .map(|m| m.path.as_path());
    let applied: Vec<(i64, Option<i64>)> = failure
        .applied
        .iter()
        .map(|d| (d.info.version, d.execution_time))
        .collect();
    let error = cause.value(py).clone().into_any();
    match migration_failed_error(
        py,
        failure.message,
        failure.version,
        path,
        failure.dirty,
        &applied,
        error,
    ) {
        Ok(failed) => {
            failed.set_cause(py, Some(cause));
            failed
        }
        Err(e) => e,
    }
}

/// `DirtyMigrationError` with a backend's own explanation of why the version is dirty.
#[must_use]
pub fn dirty_error(py: Python<'_>, message: &str, version: i64) -> PyErr {
    build(
        py,
        &DIRTY_MIGRATION_ERROR,
        "DirtyMigrationError",
        (message.to_owned(), version),
    )
}

// an unknown or too new target is a bad argument; a missing `.down.sql` is a problem with the files
#[must_use]
pub fn plan_error(py: Python<'_>, err: &PlanError) -> PyErr {
    let message = err.to_string();
    match err {
        PlanError::Dirty { version } => dirty_error(py, &message, *version),
        PlanError::Missing { versions } => match PyTuple::new(py, versions) {
            Ok(versions) => build(
                py,
                &MIGRATION_MISSING_ERROR,
                "MigrationMissingError",
                (message, versions),
            ),
            Err(e) => e,
        },
        PlanError::Checksum {
            version,
            local,
            applied,
        } => build(
            py,
            &MIGRATION_CHECKSUM_ERROR,
            "MigrationChecksumError",
            (
                message,
                *version,
                PyBytes::new(py, local),
                PyBytes::new(py, applied),
            ),
        ),
        PlanError::Order { pending, latest } => build(
            py,
            &MIGRATION_ORDER_ERROR,
            "MigrationOrderError",
            (message, *pending, *latest),
        ),
        PlanError::TargetNotFound { .. } | PlanError::TargetTooNew { .. } => {
            PyValueError::new_err(message)
        }
        PlanError::MissingDown { version } => build(
            py,
            &IRREVERSIBLE_MIGRATION_ERROR,
            "IrreversibleMigrationError",
            (message, *version),
        ),
    }
}

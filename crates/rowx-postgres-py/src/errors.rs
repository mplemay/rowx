use std::sync::Arc;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use rowx_core::error::CoreError;
use rowx_core::migrate::{Migration, NameError};
use rowx_core::pool::OptionsError;
use rowx_postgres::error::Error;
use rowx_postgres::migrate::MigrateError;
use rowx_py_core::errors as shared;

use crate::named::{RowShape, render_null, render_shape};

// the only place a Postgres driver error becomes a Python exception
#[must_use]
pub fn to_py_err(py: Python<'_>, err: &Error) -> PyErr {
    match err {
        Error::Db {
            sqlstate,
            message,
            detail,
            hint,
            position,
            routine: _,
        } => shared::database_error(
            py,
            sqlstate,
            message,
            detail.as_deref(),
            hint.as_deref(),
            *position,
        ),
        Error::Interface(_) | Error::Drift(_) => shared::interface_error(py, &err.to_string()),
        Error::Core(core) => shared::core_to_py_err(py, *core),
        Error::RowShape(_) | Error::Shape { .. } | Error::NullField { .. } => {
            shared::row_shape_error(py, &err.to_string())
        }
        Error::Decode(_) => shared::decode_error(py, &err.to_string()),
        Error::Encode(_) => shared::encode_error(py, &err.to_string()),
    }
}

/// `to_py_err`, except that a named row's mapping and NULL failures are worded with the row type's
/// name and field keys.
#[must_use]
pub fn named_to_py_err(py: Python<'_>, err: &Error, shape: &RowShape) -> PyErr {
    match err {
        Error::Shape { errors, columns } => shared::row_shape_error(
            py,
            &render_shape(shape.type_name(), shape.key_names(), errors, columns),
        ),
        Error::NullField {
            column,
            field,
            row,
            name,
        } => {
            let key = shape.key_names().get(*field).map_or("", String::as_str);
            shared::row_shape_error(
                py,
                &render_null(shape.type_name(), key, *column, name, *row),
            )
        }
        other => to_py_err(py, other),
    }
}

/// A named row whose constructor or validator raised `cause`, an `Exception`.
#[must_use]
pub fn construct_to_py_err(
    py: Python<'_>,
    cause: PyErr,
    type_name: &str,
    row_index: usize,
) -> PyErr {
    let detail = cause.value(py).repr().map_or_else(
        |_| "<unrepresentable>".to_owned(),
        |r| r.to_string_lossy().into_owned(),
    );
    shared::row_construct_error(
        py,
        &format!("{type_name} row {row_index}: {detail}"),
        type_name,
        row_index,
        cause,
    )
}

/// Carries a driver error across an `async` block, where the GIL is not held.
#[derive(Debug)]
pub struct PgError(pub Error);

impl From<Error> for PgError {
    fn from(err: Error) -> Self {
        Self(err)
    }
}

impl From<PgError> for PyErr {
    fn from(err: PgError) -> Self {
        Python::attach(|py| to_py_err(py, &err.0))
    }
}

#[must_use]
pub fn interface_error(py: Python<'_>, message: &str) -> PyErr {
    shared::interface_error(py, message)
}

#[must_use]
pub fn decode_error(py: Python<'_>, message: &str) -> PyErr {
    shared::decode_error(py, message)
}

#[must_use]
pub fn encode_error(py: Python<'_>, message: &str) -> PyErr {
    shared::encode_error(py, message)
}

impl From<CoreError> for PgError {
    fn from(err: CoreError) -> Self {
        Self(err.into())
    }
}

/// A pool setting that is out of range is a `ValueError`, like any other bad argument.
#[must_use]
pub fn options_error(err: &OptionsError) -> PyErr {
    PyValueError::new_err(err.to_string())
}

// the only place a migration error becomes a Python exception
#[must_use]
pub fn migrate_to_py_err(
    py: Python<'_>,
    err: &MigrateError,
    migrations: &[Migration],
    reverting: bool,
) -> PyErr {
    match err {
        MigrateError::Source(e) => shared::source_error(py, e),
        MigrateError::Plan(e) => shared::plan_error(py, e),
        MigrateError::Failed {
            version,
            source,
            dirty,
            applied,
            ..
        } => {
            let cause = to_py_err(py, source);
            // only a server error is a failure of the file itself; anything else (a lost connection)
            // is raised as it is
            if !matches!(source, Error::Db { .. }) {
                return cause;
            }
            // a revert runs the `.down.sql` file, everything else the forward one
            let path = migrations
                .iter()
                .find(|m| m.version == *version && m.kind.is_down() == reverting)
                .map(|m| m.path.as_path());
            let applied: Vec<(i64, Option<i64>)> = applied
                .iter()
                .map(|d| (d.info.version, d.execution_time))
                .collect();
            let error = cause.value(py).clone().into_any();
            match shared::migration_failed_error(
                py,
                &err.to_string(),
                *version,
                path,
                *dirty,
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
        MigrateError::Connection(e) => to_py_err(py, e),
        MigrateError::Cancelled => shared::interface_error(py, &err.to_string()),
        MigrateError::Unclean(inner) => migrate_to_py_err(py, inner, migrations, reverting),
    }
}

/// A bad `table` or `create_schemas` name is a `ValueError`.
#[must_use]
pub fn name_to_py_err(err: &NameError) -> PyErr {
    PyValueError::new_err(err.to_string())
}

/// Carries a migration error across an `async` block, where the GIL is not held. It also carries
/// what the error needs to name the file that failed.
#[derive(Debug)]
pub struct MigrateFailure {
    pub error: MigrateError,
    pub migrations: Arc<Vec<Migration>>,
    pub reverting: bool,
}

impl From<MigrateFailure> for PyErr {
    fn from(err: MigrateFailure) -> Self {
        Python::attach(|py| migrate_to_py_err(py, &err.error, &err.migrations, err.reverting))
    }
}

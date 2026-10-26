use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use rowx_core::error::CoreError;
use rowx_core::pool::OptionsError;
use rowx_postgres::error::Error;
use rowx_py_core::errors as shared;

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
        Error::Interface(_) => shared::interface_error(py, &err.to_string()),
        Error::Core(core) => shared::core_to_py_err(py, *core),
        Error::RowShape(_) => shared::row_shape_error(py, &err.to_string()),
        Error::Decode(_) => shared::decode_error(py, &err.to_string()),
        Error::Encode(_) => shared::encode_error(py, &err.to_string()),
    }
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

use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::PyType;
use rowx_pg::error::Error;

static INTERFACE_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static DATABASE_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static BUSY_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static ROW_SHAPE_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static DECODE_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();

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
    class(py, &ROW_SHAPE_ERROR, "RowShapeError")?;
    class(py, &DECODE_ERROR, "DecodeError")?;
    Ok(())
}

// the only place a driver error becomes a Python exception
#[must_use]
pub fn to_py_err(py: Python<'_>, err: &Error) -> PyErr {
    match map(py, err) {
        Ok(e) | Err(e) => e,
    }
}

fn map(py: Python<'_>, err: &Error) -> PyResult<PyErr> {
    let instance = match err {
        Error::Db {
            sqlstate,
            message,
            detail,
            hint,
            position,
            routine: _,
        } => class(py, &DATABASE_ERROR, "DatabaseError")?.call1((
            &**sqlstate,
            &**message,
            detail.as_deref(),
            hint.as_deref(),
            *position,
        ))?,
        Error::Interface(_) | Error::Closed | Error::Broken => {
            class(py, &INTERFACE_ERROR, "InterfaceError")?.call1((err.to_string(),))?
        }
        Error::Busy => class(py, &BUSY_ERROR, "ConnectionBusyError")?.call1((err.to_string(),))?,
        Error::RowShape(_) => {
            class(py, &ROW_SHAPE_ERROR, "RowShapeError")?.call1((err.to_string(),))?
        }
        Error::Decode(_) => class(py, &DECODE_ERROR, "DecodeError")?.call1((err.to_string(),))?,
    };
    Ok(PyErr::from_value(instance))
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
    to_py_err(py, &Error::Interface(message.to_owned()))
}

#[must_use]
pub fn decode_error(py: Python<'_>, message: &str) -> PyErr {
    to_py_err(py, &Error::Decode(message.to_owned()))
}

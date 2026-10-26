use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::PyType;
use rowx_core::error::CoreError;

static INTERFACE_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static DATABASE_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static POOL_TIMEOUT_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static BUSY_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static ROW_SHAPE_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static DECODE_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static ENCODE_ERROR: PyOnceLock<Py<PyType>> = PyOnceLock::new();

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

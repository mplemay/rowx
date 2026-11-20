use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBytes, PyDict, PyType};
use rowx_sqlite::exec::Param;
use rowx_sqlite::value::uuid::{self, Uuid};

use super::classes::class;
use crate::errors::encode_error;

static UUID: PyOnceLock<Py<PyType>> = PyOnceLock::new();

/// # Errors
/// An import error when `uuid` cannot be loaded.
pub fn uuid_class(py: Python<'_>) -> PyResult<&Bound<'_, PyType>> {
    class(py, &UUID, "uuid", "UUID")
}

/// # Errors
/// An error from `uuid.UUID` itself.
pub fn decode<'py>(py: Python<'py>, value: &Uuid) -> PyResult<Bound<'py, PyAny>> {
    let kwargs = PyDict::new(py);
    kwargs.set_item("bytes", PyBytes::new(py, &value.0))?;
    uuid_class(py)?.call((), Some(&kwargs))
}

/// # Errors
/// `EncodeError` when `bytes` is not 16 bytes long.
pub fn encode(py: Python<'_>, obj: &Bound<'_, PyAny>) -> PyResult<Param> {
    let raw = obj.getattr("bytes")?;
    let bytes: [u8; 16] = raw
        .cast::<PyBytes>()?
        .as_bytes()
        .try_into()
        .map_err(|_| encode_error(py, "a UUID needs 16 bytes"))?;
    Ok(uuid::encode(&Uuid(bytes)))
}

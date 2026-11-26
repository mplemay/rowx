use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBytes, PyDict, PyType};
use rowx_sqlite::exec::Param;
use rowx_sqlite::value::uuid::{self, Uuid};

use super::classes::{class, field};
use rowx_py_core::errors::encode_error;

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

/// The value is the `int` slot read through `UUID`'s own descriptor, because `bytes` is a property a subclass can
/// override.
///
/// # Errors
/// `EncodeError` when the value is not a 128-bit integer.
pub fn encode(py: Python<'_>, obj: &Bound<'_, PyAny>) -> PyResult<Param> {
    let value = field(obj, uuid_class(py)?, "int")?
        .extract::<u128>()
        .map_err(|_| encode_error(py, "a UUID needs a 128-bit value"))?;
    Ok(uuid::encode(&Uuid(value.to_be_bytes())))
}

use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBytes, PyDict, PyType};
use rowx_postgres::exec::Param;
use rowx_postgres::wire::uuid;

use crate::codec::extract::PyParam;
use crate::errors::decode_error;

static UUID: PyOnceLock<Py<PyType>> = PyOnceLock::new();

/// # Errors
/// An import error when `uuid` cannot be loaded.
pub fn uuid_class(py: Python<'_>) -> PyResult<&Bound<'_, PyType>> {
    let cls = UUID.get_or_try_init(py, || {
        Ok::<_, PyErr>(
            py.import("uuid")?
                .getattr("UUID")?
                .cast_into::<PyType>()?
                .unbind(),
        )
    })?;
    Ok(cls.bind(py))
}

/// # Errors
/// `DecodeError` when the length is not 16 bytes.
pub fn decode<'py>(py: Python<'py>, raw: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    if raw.len() != 16 {
        return Err(decode_error(
            py,
            &format!("uuid needs 16 bytes, got {}", raw.len()),
        ));
    }
    let kwargs = PyDict::new(py);
    kwargs.set_item("bytes", PyBytes::new(py, raw))?;
    uuid_class(py)?.call((), Some(&kwargs))
}

/// A scalar `UUID` goes out as 16 binary bytes and a scalar `str` as text for the server to parse; as an
/// array element a `str` is parsed here, so the element is always 16 bytes.
///
/// # Errors
/// A description of why the value cannot be encoded as `uuid`.
pub fn encode(param: &PyParam, element: bool) -> Result<Param, String> {
    match param {
        PyParam::Uuid(bytes) => Ok(Param::binary(bytes.to_vec())),
        PyParam::Str(s) if element => uuid::parse(s)
            .map(|bytes| Param::binary(bytes.to_vec()))
            .map_err(|e| e.to_string()),
        PyParam::Str(s) => Ok(Param::text(s.clone().into_bytes())),
        _ => Err(format!(
            "a Python {} cannot be encoded as uuid",
            param.type_name()
        )),
    }
}

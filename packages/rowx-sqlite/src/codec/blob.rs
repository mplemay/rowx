use pyo3::prelude::*;
use pyo3::types::{PyByteArray, PyBytes};
use rowx_sqlite::exec::Param;
use rowx_sqlite::value::blob;

#[must_use]
pub fn decode<'py>(py: Python<'py>, value: &[u8]) -> Bound<'py, PyAny> {
    PyBytes::new(py, value).into_any()
}

/// A `bytes`, `bytearray` or `memoryview`.
///
/// # Errors
/// A Python error when a `memoryview` cannot be read.
pub fn encode(py: Python<'_>, obj: &Bound<'_, PyAny>) -> PyResult<Param> {
    if let Ok(bytes) = obj.cast::<PyBytes>() {
        return Ok(blob::encode(bytes.as_bytes().to_vec()));
    }
    if let Ok(array) = obj.cast::<PyByteArray>() {
        return Ok(blob::encode(array.to_vec()));
    }
    let copy = py.get_type::<PyBytes>().call1((obj,))?;
    Ok(blob::encode(copy.cast::<PyBytes>()?.as_bytes().to_vec()))
}

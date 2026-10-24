use pyo3::prelude::*;
use pyo3::types::PyString;
use rowx_pg::Type;

use crate::errors::decode_error;

/// # Errors
/// `DecodeError` when the bytes are not valid UTF-8.
pub fn decode<'py>(py: Python<'py>, ty: &Type, raw: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    let text = std::str::from_utf8(raw)
        .map_err(|e| decode_error(py, &format!("{} is not valid UTF-8: {e}", ty.name())))?;
    Ok(PyString::new(py, text).into_any())
}

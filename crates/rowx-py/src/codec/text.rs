use pyo3::prelude::*;
use pyo3::types::PyString;
use rowx_pg::Type;

use crate::codec::extract::PyParam;
use crate::errors::decode_error;

/// # Errors
/// `DecodeError` when the bytes are not valid UTF-8.
pub fn decode<'py>(py: Python<'py>, ty: &Type, raw: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    let text = std::str::from_utf8(raw)
        .map_err(|e| decode_error(py, &format!("{} is not valid UTF-8: {e}", ty.name())))?;
    Ok(PyString::new(py, text).into_any())
}

/// # Errors
/// A description of why the value cannot be encoded as `ty`.
pub fn encode(ty: &Type, param: &PyParam) -> Result<Vec<u8>, String> {
    match param {
        // Postgres text cannot hold NUL, and would fail at Bind otherwise
        PyParam::Str(s) if s.contains('\0') => Err(format!(
            "a str containing NUL cannot be stored in {}",
            ty.name()
        )),
        PyParam::Str(s) => Ok(s.as_bytes().to_vec()),
        _ => Err(format!(
            "a Python {} cannot be encoded as {}",
            param.type_name(),
            ty.name()
        )),
    }
}

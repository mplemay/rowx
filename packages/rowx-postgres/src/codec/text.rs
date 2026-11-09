use pyo3::prelude::*;
use pyo3::types::PyString;
use rowx_postgres::Type;

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

/// # Errors
/// `DecodeError` when the value is not one byte of valid UTF-8.
pub fn decode_char<'py>(py: Python<'py>, raw: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    if raw.len() != 1 {
        return Err(decode_error(
            py,
            &format!("\"char\" needs 1 byte, got {}", raw.len()),
        ));
    }
    let text = std::str::from_utf8(raw)
        .map_err(|e| decode_error(py, &format!("\"char\" is not valid UTF-8: {e}")))?;
    Ok(PyString::new(py, text).into_any())
}

/// # Errors
/// A description of why the value cannot be encoded as `"char"`.
pub fn encode_char(param: &PyParam) -> Result<Vec<u8>, String> {
    match param {
        // "char" is a single byte, so a multi-byte character cannot fit
        PyParam::Str(s) if s.len() == 1 => Ok(s.as_bytes().to_vec()),
        PyParam::Str(_) => Err("a str for \"char\" must be exactly one byte".to_owned()),
        _ => Err(format!(
            "a Python {} cannot be encoded as \"char\"",
            param.type_name()
        )),
    }
}

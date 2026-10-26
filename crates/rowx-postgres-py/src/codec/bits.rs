use pyo3::prelude::*;
use pyo3::types::PyString;
use rowx_postgres::Type;
use rowx_postgres::exec::Param;
use rowx_postgres::wire::bits;

use crate::codec::extract::PyParam;
use crate::errors::decode_error;

/// # Errors
/// `DecodeError` when the value is malformed.
pub fn decode<'py>(py: Python<'py>, ty: &Type, raw: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    let text = bits::decode(raw)
        .map_err(|e| decode_error(py, &format!("{} is malformed: {e}", ty.name())))?;
    Ok(PyString::new(py, &text).into_any())
}

/// The server checks a `bit(n)` length, so a string of any length is encoded as is.
///
/// # Errors
/// A description of why the value cannot be encoded as `bit` or `varbit`.
pub fn encode(ty: &Type, param: &PyParam) -> Result<Param, String> {
    match param {
        PyParam::Str(text) => bits::encode(text)
            .map(Param::binary)
            .map_err(|e| e.to_string()),
        _ => Err(format!(
            "a Python {} cannot be encoded as {}",
            param.type_name(),
            ty.name()
        )),
    }
}

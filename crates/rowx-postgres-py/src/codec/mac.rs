use pyo3::prelude::*;
use pyo3::types::PyString;
use rowx_postgres::Type;
use rowx_postgres::exec::Param;
use rowx_postgres::wire::mac::{self, MAC_LEN, MAC8_LEN};

use crate::codec::extract::PyParam;
use crate::errors::decode_error;

fn width(ty: &Type) -> usize {
    if *ty == Type::MACADDR8 {
        MAC8_LEN
    } else {
        MAC_LEN
    }
}

/// # Errors
/// `DecodeError` when the length does not match the type.
pub fn decode<'py>(py: Python<'py>, ty: &Type, raw: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    let text = mac::decode(raw, width(ty)).map_err(|e| decode_error(py, &e.to_string()))?;
    Ok(PyString::new(py, &text).into_any())
}

/// The `str` goes out as text for the server to parse, as asyncpg and psycopg do, so every
/// spelling the server accepts works and it owns the error for a malformed one.
///
/// # Errors
/// A description of why the value cannot be encoded as `macaddr` or `macaddr8`.
pub fn encode(ty: &Type, param: &PyParam) -> Result<Param, String> {
    match param {
        PyParam::Str(text) if text.contains('\0') => Err("the str contains a NUL byte".to_owned()),
        PyParam::Str(text) => Ok(Param::text(text.clone().into_bytes())),
        _ => Err(format!(
            "a Python {} cannot be encoded as {}",
            param.type_name(),
            ty.name()
        )),
    }
}

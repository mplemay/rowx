pub mod int;
pub mod text;

use pyo3::prelude::*;
use rowx_pg::Type;

use crate::errors::decode_error;

/// One arm per type family; T12-T18 add the rest.
///
/// # Errors
/// `DecodeError` for an unsupported type or a malformed value.
pub fn decode<'py>(py: Python<'py>, ty: &Type, raw: Option<&[u8]>) -> PyResult<Bound<'py, PyAny>> {
    let Some(raw) = raw else {
        return Ok(py.None().into_bound(py));
    };
    match *ty {
        Type::BOOL | Type::INT2 | Type::INT4 | Type::INT8 => int::decode(py, ty, raw),
        Type::TEXT | Type::VARCHAR => text::decode(py, ty, raw),
        // void arrives as an empty, non-NULL value
        Type::VOID => Ok(py.None().into_bound(py)),
        _ => Err(decode_error(
            py,
            &format!("no decoder for the postgres type {}", ty.name()),
        )),
    }
}

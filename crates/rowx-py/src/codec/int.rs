use pyo3::prelude::*;
use pyo3::types::PyBool;
use rowx_pg::Type;

use crate::errors::decode_error;

fn fixed<const N: usize>(py: Python<'_>, ty: &Type, raw: &[u8]) -> PyResult<[u8; N]> {
    <[u8; N]>::try_from(raw).map_err(|_| {
        decode_error(
            py,
            &format!("{} needs {N} bytes, got {}", ty.name(), raw.len()),
        )
    })
}

/// # Errors
/// `DecodeError` when the value length is wrong for the type.
pub fn decode<'py>(py: Python<'py>, ty: &Type, raw: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    match *ty {
        Type::BOOL => {
            let [byte] = fixed::<1>(py, ty, raw)?;
            Ok(PyBool::new(py, byte != 0).to_owned().into_any())
        }
        Type::INT2 => Ok(i16::from_be_bytes(fixed(py, ty, raw)?)
            .into_pyobject(py)?
            .into_any()),
        Type::INT4 => Ok(i32::from_be_bytes(fixed(py, ty, raw)?)
            .into_pyobject(py)?
            .into_any()),
        _ => Ok(i64::from_be_bytes(fixed(py, ty, raw)?)
            .into_pyobject(py)?
            .into_any()),
    }
}

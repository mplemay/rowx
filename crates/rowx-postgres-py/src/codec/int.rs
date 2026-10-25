use pyo3::prelude::*;
use pyo3::types::PyBool;
use rowx_postgres::Type;

use crate::codec::extract::PyParam;
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

/// # Errors
/// A description of why the value cannot be encoded as `ty`.
pub fn encode(ty: &Type, param: &PyParam) -> Result<Vec<u8>, String> {
    let out_of_range = || format!("the int is out of range for {}", ty.name());
    match (ty, param) {
        (&Type::BOOL, PyParam::Bool(b)) => Ok(vec![u8::from(*b)]),
        (&Type::INT2, PyParam::Int(i)) => i16::try_from(*i)
            .map(|v| v.to_be_bytes().to_vec())
            .map_err(|_| out_of_range()),
        (&Type::INT4, PyParam::Int(i)) => i32::try_from(*i)
            .map(|v| v.to_be_bytes().to_vec())
            .map_err(|_| out_of_range()),
        (&Type::INT8, PyParam::Int(i)) => Ok(i.to_be_bytes().to_vec()),
        (_, PyParam::BigInt(_)) => Err(out_of_range()),
        _ => Err(format!(
            "a Python {} cannot be encoded as {}",
            param.type_name(),
            ty.name()
        )),
    }
}

use pyo3::prelude::*;
use pyo3::types::PyFloat;
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
    let value = match *ty {
        Type::FLOAT4 => f64::from(f32::from_be_bytes(fixed(py, ty, raw)?)),
        _ => f64::from_be_bytes(fixed(py, ty, raw)?),
    };
    Ok(PyFloat::new(py, value).into_any())
}

// an int above 2^53 loses precision, which the design documents
#[allow(clippy::cast_precision_loss)]
const fn int_to_f64(value: i64) -> f64 {
    value as f64
}

// narrowing is the point; overflow to infinity is rejected by the caller
#[allow(clippy::cast_possible_truncation)]
const fn f64_to_f32(value: f64) -> f32 {
    value as f32
}

fn as_f64(ty: &Type, param: &PyParam) -> Result<f64, String> {
    match param {
        PyParam::Float(f) => Ok(*f),
        PyParam::Int(i) => Ok(int_to_f64(*i)),
        PyParam::BigInt(digits) => digits
            .parse::<f64>()
            .ok()
            .filter(|f| f.is_finite())
            .ok_or_else(|| format!("the int is out of range for {}", ty.name())),
        _ => Err(format!(
            "a Python {} cannot be encoded as {}",
            param.type_name(),
            ty.name()
        )),
    }
}

/// # Errors
/// A description of why the value cannot be encoded as `ty`.
pub fn encode(ty: &Type, param: &PyParam) -> Result<Vec<u8>, String> {
    let value = as_f64(ty, param)?;
    if *ty == Type::FLOAT4 {
        let narrowed = f64_to_f32(value);
        if narrowed.is_infinite() && value.is_finite() {
            return Err(format!("the value is out of range for {}", ty.name()));
        }
        return Ok(narrowed.to_be_bytes().to_vec());
    }
    Ok(value.to_be_bytes().to_vec())
}

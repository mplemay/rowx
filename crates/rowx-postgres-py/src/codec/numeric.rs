use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::PyType;
use rowx_postgres::wire::numeric::{self, DecimalExponent, Numeric, NumericError};

use crate::codec::extract::PyParam;
use crate::errors::decode_error;

static DECIMAL: PyOnceLock<Py<PyType>> = PyOnceLock::new();

/// # Errors
/// An import error when `decimal` cannot be loaded.
pub fn decimal_class(py: Python<'_>) -> PyResult<&Bound<'_, PyType>> {
    let cls = DECIMAL.get_or_try_init(py, || {
        Ok::<_, PyErr>(
            py.import("decimal")?
                .getattr("Decimal")?
                .cast_into::<PyType>()?
                .unbind(),
        )
    })?;
    Ok(cls.bind(py))
}

/// # Errors
/// `DecodeError` when the bytes are not a well-formed binary numeric.
pub fn decode<'py>(py: Python<'py>, raw: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    let value = numeric::decode(raw)
        .map_err(|e| decode_error(py, &format!("numeric is malformed: {e}")))?;
    decimal_class(py)?.call1((value.to_decimal_string(),))
}

/// # Errors
/// `DecodeError` when the length is not 4 bytes.
pub fn decode_oid<'py>(py: Python<'py>, raw: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    let bytes = <[u8; 4]>::try_from(raw)
        .map_err(|_| decode_error(py, &format!("oid needs 4 bytes, got {}", raw.len())))?;
    Ok(u32::from_be_bytes(bytes).into_pyobject(py)?.into_any())
}

/// # Errors
/// A description of why the value cannot be encoded as `oid`.
pub fn encode_oid(param: &PyParam) -> Result<Vec<u8>, String> {
    match param {
        PyParam::Int(i) => u32::try_from(*i)
            .map(|v| v.to_be_bytes().to_vec())
            .map_err(|_| "the int is out of range for oid".to_owned()),
        PyParam::BigInt(_) => Err("the int is out of range for oid".to_owned()),
        _ => Err(format!(
            "a Python {} cannot be encoded as oid",
            param.type_name()
        )),
    }
}

fn digits_of(mut magnitude: u128) -> Vec<u8> {
    let mut digits = Vec::new();
    while magnitude > 0 {
        digits.push(u8::try_from(magnitude % 10).unwrap_or(0));
        magnitude /= 10;
    }
    digits.reverse();
    digits
}

fn parts_of(param: &PyParam) -> Result<(bool, Vec<u8>, DecimalExponent), String> {
    Ok(match param {
        PyParam::Int(i) => (
            *i < 0,
            digits_of(u128::from(i.unsigned_abs())),
            DecimalExponent::Finite(0),
        ),
        PyParam::BigInt(text) => {
            let (negative, magnitude) = text
                .strip_prefix('-')
                .map_or((false, text.as_str()), |rest| (true, rest));
            let digits = magnitude
                .bytes()
                .map(|b| b.is_ascii_digit().then_some(b - b'0'))
                .collect::<Option<Vec<u8>>>()
                .ok_or_else(|| "the int is not a decimal number".to_owned())?;
            (negative, digits, DecimalExponent::Finite(0))
        }
        PyParam::Decimal {
            negative,
            digits,
            exponent,
        } => (*negative, digits.clone(), *exponent),
        _ => {
            return Err(format!(
                "a Python {} cannot be encoded as numeric",
                param.type_name()
            ));
        }
    })
}

fn numeric_of(param: &PyParam) -> Result<Numeric, String> {
    let (negative, digits, exponent) = parts_of(param)?;
    Numeric::from_decimal_parts(negative, &digits, exponent)
        .map_err(|e: NumericError| e.to_string())
}

/// Text form of the value, since a scalar numeric is sent in text format.
///
/// # Errors
/// A description of why the value cannot be encoded as `numeric`.
pub fn encode(param: &PyParam) -> Result<Vec<u8>, String> {
    let (negative, digits, exponent) = parts_of(param)?;
    numeric::decimal_text(negative, &digits, exponent)
        .map(String::into_bytes)
        .map_err(|e: NumericError| e.to_string())
}

/// Binary form, for an array element or a range bound.
///
/// # Errors
/// A description of why the value cannot be encoded as `numeric`.
pub fn encode_binary(param: &PyParam) -> Result<Vec<u8>, String> {
    numeric_of(param)?.encode().map_err(|e| e.to_string())
}

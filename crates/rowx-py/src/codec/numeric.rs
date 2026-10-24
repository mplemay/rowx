use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::PyType;
use rowx_pg::wire::numeric::{self, NumericError};

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

/// Text form of the value, since numeric is sent in text format.
///
/// # Errors
/// A description of why the value cannot be encoded as `numeric`.
pub fn encode(param: &PyParam) -> Result<Vec<u8>, String> {
    let text = match param {
        PyParam::Int(i) => i.to_string(),
        PyParam::BigInt(digits) => digits.clone(),
        PyParam::Decimal(text) => {
            numeric::normalize_decimal_text(text).map_err(|e: NumericError| e.to_string())?
        }
        _ => {
            return Err(format!(
                "a Python {} cannot be encoded as numeric",
                param.type_name()
            ));
        }
    };
    Ok(text.into_bytes())
}

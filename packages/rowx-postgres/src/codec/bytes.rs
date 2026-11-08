use pyo3::prelude::*;
use pyo3::types::PyBytes;

use crate::codec::extract::PyParam;

#[must_use]
pub fn decode<'py>(py: Python<'py>, raw: &[u8]) -> Bound<'py, PyAny> {
    PyBytes::new(py, raw).into_any()
}

/// # Errors
/// A description of why the value cannot be encoded as `bytea`.
pub fn encode(param: &PyParam) -> Result<Vec<u8>, String> {
    match param {
        PyParam::Bytes(data, _) => Ok(data.clone()),
        _ => Err(format!(
            "a Python {} cannot be encoded as bytea",
            param.type_name()
        )),
    }
}

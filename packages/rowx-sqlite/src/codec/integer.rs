use pyo3::exceptions::PyOverflowError;
use pyo3::prelude::*;
use rowx_sqlite::exec::Param;
use rowx_sqlite::value::integer;

use crate::errors::encode_error;

/// # Errors
/// Never in practice; the conversion cannot fail.
pub fn decode(py: Python<'_>, value: i64) -> PyResult<Bound<'_, PyAny>> {
    Ok(value.into_pyobject(py)?.into_any())
}

/// # Errors
/// `EncodeError` for an int outside the 64-bit signed range.
pub fn encode(py: Python<'_>, obj: &Bound<'_, PyAny>) -> PyResult<Param> {
    match obj.extract::<i64>() {
        Ok(value) => Ok(integer::encode(value)),
        Err(err) if err.is_instance_of::<PyOverflowError>(py) => Err(encode_error(
            py,
            "the int is outside the SQLite INTEGER range (64-bit signed)",
        )),
        Err(err) => Err(err),
    }
}

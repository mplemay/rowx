use pyo3::prelude::*;
use pyo3::types::PyFloat;
use rowx_sqlite::exec::Param;
use rowx_sqlite::value::real;

use rowx_py_core::errors::encode_error;

#[must_use]
pub fn decode(py: Python<'_>, value: f64) -> Bound<'_, PyAny> {
    PyFloat::new(py, value).into_any()
}

/// # Errors
/// `EncodeError` for NaN.
pub fn encode(py: Python<'_>, obj: &Bound<'_, PyAny>) -> PyResult<Param> {
    real::encode(obj.extract::<f64>()?).map_err(|e| encode_error(py, &e.to_string()))
}

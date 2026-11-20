use pyo3::prelude::*;
use pyo3::types::PyString;
use rowx_sqlite::exec::Param;
use rowx_sqlite::value::text;

use rowx_py_core::errors::encode_error;

#[must_use]
pub fn decode<'py>(py: Python<'py>, value: &str) -> Bound<'py, PyAny> {
    PyString::new(py, value).into_any()
}

/// # Errors
/// `EncodeError` for a lone surrogate or an embedded NUL.
pub fn encode(py: Python<'_>, obj: &Bound<'_, PyString>) -> PyResult<Param> {
    // a lone surrogate has no UTF-8 form
    let value = obj
        .to_str()
        .map_err(|_| encode_error(py, "a str with a lone surrogate cannot be stored"))?;
    text::encode(value).map_err(|e| encode_error(py, &e.to_string()))
}

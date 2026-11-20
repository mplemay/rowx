use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::PyType;
use rowx_sqlite::exec::Param;
use rowx_sqlite::value::decimal::{self, Decimal};

use super::classes::class;
use rowx_py_core::errors::encode_error;

static DECIMAL: PyOnceLock<Py<PyType>> = PyOnceLock::new();

/// # Errors
/// An import error when `decimal` cannot be loaded.
pub fn decimal_class(py: Python<'_>) -> PyResult<&Bound<'_, PyType>> {
    class(py, &DECIMAL, "decimal", "Decimal")
}

/// # Errors
/// An error from `decimal.Decimal` itself.
pub fn decode<'py>(py: Python<'py>, value: &Decimal) -> PyResult<Bound<'py, PyAny>> {
    decimal_class(py)?.call1((value.as_str(),))
}

/// # Errors
/// `EncodeError` for NaN and infinities.
pub fn encode(py: Python<'_>, obj: &Bound<'_, PyAny>) -> PyResult<Param> {
    let text = obj.str()?;
    let parsed = Decimal::parse(&text.to_cow()?).ok_or_else(|| {
        encode_error(py, "a Decimal that is not a finite number cannot be stored")
    })?;
    Ok(decimal::encode(&parsed))
}

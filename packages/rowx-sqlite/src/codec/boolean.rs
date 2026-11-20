use pyo3::prelude::*;
use pyo3::types::PyBool;
use rowx_sqlite::exec::Param;
use rowx_sqlite::value::boolean;

#[must_use]
pub fn decode(py: Python<'_>, value: bool) -> Bound<'_, PyAny> {
    PyBool::new(py, value).to_owned().into_any()
}

#[must_use]
pub fn encode(value: bool) -> Param {
    boolean::encode(value)
}

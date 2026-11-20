use pyo3::exceptions::{PyRecursionError, PyValueError};
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::PyDict;
use rowx_sqlite::exec::Param;
use rowx_sqlite::value::json;

use crate::errors::{decode_error, encode_error};

static LOADS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static DUMPS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

fn json_fn<'py>(
    py: Python<'py>,
    cell: &'static PyOnceLock<Py<PyAny>>,
    name: &str,
) -> PyResult<&'py Bound<'py, PyAny>> {
    let func = cell.get_or_try_init(py, || {
        Ok::<_, PyErr>(py.import("json")?.getattr(name)?.unbind())
    })?;
    Ok(func.bind(py))
}

/// # Errors
/// `DecodeError` for text that is not valid JSON.
pub fn decode<'py>(py: Python<'py>, text: &str) -> PyResult<Bound<'py, PyAny>> {
    json_fn(py, &LOADS, "loads")?.call1((text,)).map_err(|e| {
        // only bad input is a decode error; MemoryError, KeyboardInterrupt and the like propagate
        if e.is_instance_of::<PyValueError>(py) || e.is_instance_of::<PyRecursionError>(py) {
            decode_error(py, &format!("the stored value is not valid JSON: {e}"))
        } else {
            e
        }
    })
}

/// A `dict`, `list` or `tuple` becomes JSON text.
///
/// # Errors
/// `EncodeError` for a value `json.dumps` cannot serialise (NaN included, which is not JSON).
pub fn encode(py: Python<'_>, obj: &Bound<'_, PyAny>) -> PyResult<Param> {
    let kwargs = PyDict::new(py);
    kwargs.set_item("allow_nan", false)?;
    let text = json_fn(py, &DUMPS, "dumps")?
        .call((obj,), Some(&kwargs))
        .and_then(|out| out.extract::<String>())
        .map_err(|e| {
            if e.is_instance_of::<PyValueError>(py)
                || e.is_instance_of::<PyRecursionError>(py)
                || e.is_instance_of::<pyo3::exceptions::PyTypeError>(py)
            {
                encode_error(py, &format!("the value is not JSON serialisable: {e}"))
            } else {
                e
            }
        })?;
    json::encode(&text).map_err(|e| encode_error(py, &e.to_string()))
}

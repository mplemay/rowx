use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyCFunction, PyDict, PyTuple};

static DECODER: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

// `NaN`, `Infinity` and `-Infinity` are not JSON, and the encoders refuse them (`allow_nan=False`)
fn reject_constant(args: &Bound<'_, PyTuple>, _kwargs: Option<&Bound<'_, PyDict>>) -> PyResult<()> {
    let constant: String = args.get_item(0)?.extract()?;
    Err(PyValueError::new_err(format!(
        "{constant} is not a JSON number"
    )))
}

fn decoder(py: Python<'_>) -> PyResult<&Bound<'_, PyAny>> {
    let decoder = DECODER.get_or_try_init(py, || {
        let kwargs = PyDict::new(py);
        kwargs.set_item(
            "parse_constant",
            PyCFunction::new_closure(py, None, None, reject_constant)?,
        )?;
        Ok::<_, PyErr>(
            py.import("json")?
                .getattr("JSONDecoder")?
                .call((), Some(&kwargs))?
                .unbind(),
        )
    })?;
    Ok(decoder.bind(py))
}

/// `json.loads` for stored text, except that the non-finite constants are rejected.
///
/// # Errors
/// `ValueError` (`json.JSONDecodeError` included) for text that is not JSON, `RecursionError` for nesting that is
/// too deep, and whatever else the decoder raises.
pub fn loads<'py>(py: Python<'py>, text: &str) -> PyResult<Bound<'py, PyAny>> {
    decoder(py)?.call_method1("decode", (text,))
}

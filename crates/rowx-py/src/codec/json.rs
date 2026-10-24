use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBool, PyFloat, PyInt, PyString};
use rowx_pg::Type;

use crate::codec::extract::PyParam;
use crate::codec::numeric::decimal_class;
use crate::errors::decode_error;

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
/// `DecodeError` for a bad jsonb version byte, invalid UTF-8 or invalid JSON.
pub fn decode<'py>(py: Python<'py>, ty: &Type, raw: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    let body = if *ty == Type::JSONB {
        match raw.split_first() {
            Some((1, rest)) => rest,
            _ => {
                return Err(decode_error(
                    py,
                    "jsonb has an unsupported version byte; cast to text to read it",
                ));
            }
        }
    } else {
        raw
    };
    let text = std::str::from_utf8(body)
        .map_err(|e| decode_error(py, &format!("{} is not valid UTF-8: {e}", ty.name())))?;
    json_fn(py, &LOADS, "loads")?
        .call1((text,))
        .map_err(|e| decode_error(py, &format!("{} is not valid JSON: {e}", ty.name())))
}

fn to_object<'py>(py: Python<'py>, param: &PyParam) -> Result<Bound<'py, PyAny>, String> {
    let built = match param {
        PyParam::Bool(b) => Ok::<_, PyErr>(PyBool::new(py, *b).to_owned().into_any()),
        PyParam::Int(i) => i
            .into_pyobject(py)
            .map(Bound::into_any)
            .map_err(PyErr::from),
        PyParam::BigInt(digits) => py.get_type::<PyInt>().call1((digits.as_str(),)),
        PyParam::Float(f) => Ok(PyFloat::new(py, *f).into_any()),
        PyParam::Decimal(text) => decimal_class(py).and_then(|cls| cls.call1((text.as_str(),))),
        PyParam::Str(s) => Ok(PyString::new(py, s).into_any()),
        PyParam::Seq(_, obj, _) | PyParam::Other(obj, _) => Ok(obj.bind(py).clone()),
        PyParam::Null
        | PyParam::Bytes(..)
        | PyParam::Uuid(_)
        | PyParam::Date(_)
        | PyParam::Time { .. }
        | PyParam::DateTime { .. }
        | PyParam::Timedelta { .. } => {
            return Err(format!(
                "a Python {} is not JSON serialisable",
                param.type_name()
            ));
        }
    };
    built.map_err(|e| e.to_string())
}

/// Always `json.dumps`, so a `str` becomes a JSON string.
///
/// # Errors
/// A description of why the value is not JSON serialisable.
pub fn encode(param: &PyParam) -> Result<Vec<u8>, String> {
    Python::attach(|py| {
        let obj = to_object(py, param)?;
        let dumps = json_fn(py, &DUMPS, "dumps").map_err(|e| e.to_string())?;
        let text = dumps
            .call1((obj,))
            .and_then(|out| out.extract::<String>())
            .map_err(|e| format!("the value is not JSON serialisable: {e}"))?;
        Ok(text.into_bytes())
    })
}

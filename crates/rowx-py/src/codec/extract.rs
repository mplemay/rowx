use pyo3::buffer::PyBuffer;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyByteArray, PyBytes, PyFloat, PyInt, PyList, PyMemoryView, PyString};

use crate::codec::numeric::decimal_class;
use crate::codec::uuid::uuid_class;
use crate::errors::encode_error;

/// A Python parameter copied out under the GIL so encoding can run without it.
pub enum PyParam {
    Null,
    Bool(bool),
    Int(i64),
    // an int outside i64, kept as its decimal text
    BigInt(String),
    Float(f64),
    // the `str()` of a `decimal.Decimal`
    Decimal(String),
    Str(String),
    // bytes, bytearray or memoryview contents, with the Python type name
    Bytes(Vec<u8>, String),
    Uuid([u8; 16]),
    // anything without a dedicated variant yet, with its Python type name
    Other(Py<PyAny>, String),
}

impl std::fmt::Debug for PyParam {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "PyParam({}, <redacted>)", self.type_name())
    }
}

impl PyParam {
    #[must_use]
    pub fn type_name(&self) -> &str {
        match self {
            Self::Null => "None",
            Self::Bool(_) => "bool",
            Self::Int(_) | Self::BigInt(_) => "int",
            Self::Float(_) => "float",
            Self::Decimal(_) => "Decimal",
            Self::Str(_) => "str",
            Self::Uuid(_) => "UUID",
            Self::Bytes(_, name) | Self::Other(_, name) => name,
        }
    }
}

fn one(py: Python<'_>, index: usize, obj: &Bound<'_, PyAny>) -> PyResult<PyParam> {
    if obj.is_none() {
        return Ok(PyParam::Null);
    }
    // bool is a subclass of int, so it has to be checked first
    if obj.is_instance_of::<PyBool>() {
        return Ok(PyParam::Bool(obj.extract()?));
    }
    if let Ok(int) = obj.cast::<PyInt>() {
        return Ok(match int.extract::<i64>() {
            Ok(value) => PyParam::Int(value),
            Err(_) => PyParam::BigInt(int.str()?.to_string()),
        });
    }
    if let Ok(float) = obj.cast::<PyFloat>() {
        return Ok(PyParam::Float(float.value()));
    }
    if obj.is_instance(decimal_class(py)?)? {
        return Ok(PyParam::Decimal(obj.str()?.to_string()));
    }
    if obj.is_instance_of::<PyBytes>()
        || obj.is_instance_of::<PyByteArray>()
        || obj.is_instance_of::<PyMemoryView>()
    {
        let name = obj.get_type().name()?.to_string();
        return PyBuffer::<u8>::get(obj)
            .and_then(|buf| buf.to_vec(py))
            .map(|data| PyParam::Bytes(data, name))
            .map_err(|e| {
                encode_error(py, &format!("${}: the buffer is unusable: {e}", index + 1))
            });
    }
    if obj.is_instance(uuid_class(py)?)? {
        let raw = obj.getattr("bytes")?.extract::<Vec<u8>>()?;
        if let Ok(bytes) = <[u8; 16]>::try_from(raw) {
            return Ok(PyParam::Uuid(bytes));
        }
    }
    if let Ok(text) = obj.cast::<PyString>() {
        return text
            .to_str()
            .map(|s| PyParam::Str(s.to_owned()))
            .map_err(|e| {
                encode_error(
                    py,
                    &format!("${}: the str cannot be encoded as UTF-8: {e}", index + 1),
                )
            });
    }
    let name = obj.get_type().name()?.to_string();
    Ok(PyParam::Other(obj.clone().unbind(), name))
}

/// # Errors
/// `EncodeError` for a `str` that is not valid UTF-8 (a lone surrogate).
pub fn extract(py: Python<'_>, params: &Bound<'_, PyList>) -> PyResult<Vec<PyParam>> {
    params
        .iter()
        .enumerate()
        .map(|(index, obj)| one(py, index, &obj))
        .collect()
}

pub mod blob;
pub mod boolean;
mod classes;
pub mod decimal;
pub mod integer;
pub mod json;
pub mod real;
pub mod temporal;
pub mod text;
pub mod uuid;

use pyo3::prelude::*;
use pyo3::types::{
    PyBool, PyByteArray, PyBytes, PyDict, PyFloat, PyInt, PyList, PyMemoryView, PyString, PyTuple,
};
use rowx_sqlite::exec::Param;
use rowx_sqlite::value::Scalar;

use crate::errors::encode_error;

/// One arm per type family; NULL is `None`.
///
/// # Errors
/// A Python error from building the value, and `DecodeError` for JSON text that does not parse.
pub fn to_py<'py>(py: Python<'py>, value: Option<&Scalar>) -> PyResult<Bound<'py, PyAny>> {
    let Some(value) = value else {
        return Ok(py.None().into_bound(py));
    };
    match value {
        Scalar::Integer(int) => integer::decode(py, *int),
        Scalar::Real(float) => Ok(real::decode(py, *float)),
        Scalar::Text(text) => Ok(text::decode(py, text)),
        Scalar::Blob(bytes) => Ok(blob::decode(py, bytes)),
        Scalar::Boolean(flag) => Ok(boolean::decode(py, *flag)),
        Scalar::Date(date) => temporal::decode_date(py, *date),
        Scalar::Time(time) => temporal::decode_time(py, *time),
        Scalar::DateTime(datetime) => temporal::decode_datetime(py, *datetime),
        Scalar::Aware(aware) => temporal::decode_aware(py, *aware),
        Scalar::Decimal(decimal) => decimal::decode(py, decimal),
        Scalar::Uuid(uuid) => uuid::decode(py, uuid),
        Scalar::Json(text) => json::decode(py, text),
    }
}

/// A parameter is untyped, so its Python type picks the codec.
///
/// # Errors
/// `EncodeError` for a value that cannot be stored or a type that has no codec.
pub fn extract(py: Python<'_>, obj: &Bound<'_, PyAny>) -> PyResult<Param> {
    if obj.is_none() {
        return Ok(Param::Null);
    }
    // a bool is an int, so it goes first
    if let Ok(flag) = obj.cast::<PyBool>() {
        return Ok(boolean::encode(flag.is_true()));
    }
    if obj.is_instance_of::<PyInt>() {
        return integer::encode(py, obj);
    }
    if obj.is_instance_of::<PyFloat>() {
        return real::encode(py, obj);
    }
    if let Ok(string) = obj.cast::<PyString>() {
        return text::encode(py, string);
    }
    if obj.is_instance_of::<PyBytes>()
        || obj.is_instance_of::<PyByteArray>()
        || obj.is_instance_of::<PyMemoryView>()
    {
        return blob::encode(py, obj);
    }
    // a datetime is a date, so it goes first
    if obj.is_instance(temporal::datetime_class(py)?)? {
        return temporal::encode_datetime(py, obj);
    }
    if obj.is_instance(temporal::date_class(py)?)? {
        return temporal::encode_date(py, obj);
    }
    if obj.is_instance(temporal::time_class(py)?)? {
        return temporal::encode_time(py, obj);
    }
    if obj.is_instance(decimal::decimal_class(py)?)? {
        return decimal::encode(py, obj);
    }
    if obj.is_instance(uuid::uuid_class(py)?)? {
        return uuid::encode(py, obj);
    }
    if obj.is_instance_of::<PyDict>()
        || obj.is_instance_of::<PyList>()
        || obj.is_instance_of::<PyTuple>()
    {
        return json::encode(py, obj);
    }
    Err(encode_error(
        py,
        &format!(
            "a Python {} cannot be bound as a SQLite parameter",
            obj.get_type().name()?
        ),
    ))
}

/// # Errors
/// As `extract`; the error carries a note naming the parameter.
pub fn extract_all(py: Python<'_>, params: &Bound<'_, PyList>) -> PyResult<Vec<Param>> {
    params
        .iter()
        .enumerate()
        .map(|(index, obj)| {
            extract(py, &obj).inspect_err(|err| {
                // a note cannot fail the error it annotates
                let _ = err.value(py).call_method1(
                    "add_note",
                    (format!("while binding parameter ?{}", index + 1),),
                );
            })
        })
        .collect()
}

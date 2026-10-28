use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyDict, PyString, PyTuple, PyType};
use rowx_postgres::Type;
use rowx_postgres::exec::Param;
use rowx_postgres::wire::cube::{self, Cube};
use rowx_postgres::wire::{hstore, ltree};

use crate::codec::extract::PyParam;
use crate::errors::decode_error;

static CUBE: PyOnceLock<Py<PyType>> = PyOnceLock::new();

/// The `rowx.postgres.Cube` class.
///
/// # Errors
/// An import failure of `rowx.postgres`.
pub fn cube_class(py: Python<'_>) -> PyResult<&Bound<'_, PyType>> {
    let cls = CUBE.get_or_try_init(py, || {
        Ok::<_, PyErr>(
            py.import("rowx.postgres")?
                .getattr("Cube")?
                .cast_into::<PyType>()?
                .unbind(),
        )
    })?;
    Ok(cls.bind(py))
}

/// The extension types the dispatcher hands to this module, by name; the owner was checked at prepare.
#[must_use]
pub fn is_extension_type(ty: &Type) -> bool {
    matches!(ty.name(), "hstore" | "ltree" | "lquery" | "cube")
}

/// # Errors
/// `DecodeError` for a malformed value or an unknown `ltree`/`lquery` version.
pub fn decode<'py>(py: Python<'py>, ty: &Type, raw: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    let fail = |e: &dyn std::fmt::Display| decode_error(py, &format!("{}: {e}", ty.name()));
    match ty.name() {
        "hstore" => {
            let dict = PyDict::new(py);
            for (key, value) in hstore::decode(raw).map_err(|e| fail(&e))? {
                dict.set_item(key, value)?;
            }
            Ok(dict.into_any())
        }
        "cube" => {
            let value = cube::decode(raw).map_err(|e| fail(&e))?;
            let corner = |coords: Vec<f64>| PyTuple::new(py, coords);
            cube_class(py)?.call1((corner(value.lower_left)?, corner(value.upper_right)?))
        }
        name => {
            let label = if name == "lquery" { "lquery" } else { "ltree" };
            let text = ltree::decode(raw, label).map_err(|e| fail(&e))?;
            Ok(PyString::new(py, text).into_any())
        }
    }
}

fn hstore_pairs(dict: &Bound<'_, PyDict>) -> Result<hstore::Pairs, String> {
    let text = |item: &Bound<'_, PyAny>, what: &str| {
        item.cast::<PyString>()
            .map_err(|_| {
                format!(
                    "an hstore {what} must be a str, got a Python {}",
                    item.get_type()
                        .name()
                        .map_or_else(|_| "object".into(), |n| n.to_string())
                )
            })
            .and_then(|s| {
                s.to_str()
                    .map(str::to_owned)
                    .map_err(|e| format!("an hstore {what} is not valid UTF-8: {e}"))
            })
    };
    dict.iter()
        .map(|(key, value)| {
            if key.is_none() {
                return Err("an hstore key cannot be NULL".to_owned());
            }
            let value = if value.is_none() {
                None
            } else {
                Some(text(&value, "value")?)
            };
            Ok((text(&key, "key")?, value))
        })
        .collect()
}

/// # Errors
/// A description of why the value cannot be encoded as `ty`.
pub fn encode(ty: &Type, param: &PyParam) -> Result<Param, String> {
    let mismatch = || {
        format!(
            "a Python {} cannot be encoded as {}",
            param.type_name(),
            ty.name()
        )
    };
    match (ty.name(), param) {
        ("hstore", PyParam::Other(obj, _)) => Python::attach(|py| {
            let dict = obj.bind(py).cast::<PyDict>().map_err(|_| mismatch())?;
            hstore::encode(&hstore_pairs(dict)?)
                .map(Param::binary)
                .map_err(|e| e.to_string())
        }),
        ("cube", PyParam::Cube { lower, upper }) => cube::encode(&Cube {
            lower_left: lower.clone(),
            upper_right: upper.clone(),
        })
        .map(Param::binary)
        .map_err(|e| e.to_string()),
        ("ltree" | "lquery", PyParam::Str(text)) if text.contains('\0') => Err(format!(
            "a str containing NUL cannot be stored in {}",
            ty.name()
        )),
        ("ltree" | "lquery", PyParam::Str(text)) => Ok(Param::binary(ltree::encode(text))),
        _ => Err(mismatch()),
    }
}

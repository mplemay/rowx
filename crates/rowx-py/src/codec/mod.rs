pub mod extract;
pub mod float;
pub mod int;
pub mod numeric;
pub mod text;

use pyo3::prelude::*;
use rowx_pg::Type;
use rowx_pg::error::Error;
use rowx_pg::exec::Param;
use rowx_pg::types::{Matcher, TYPE_TABLE};

pub use extract::{PyParam, extract};

use crate::errors::decode_error;

/// One arm per type family; T12-T18 add the rest.
///
/// # Errors
/// `DecodeError` for an unsupported type or a malformed value.
pub fn decode<'py>(py: Python<'py>, ty: &Type, raw: Option<&[u8]>) -> PyResult<Bound<'py, PyAny>> {
    let Some(raw) = raw else {
        return Ok(py.None().into_bound(py));
    };
    match *ty {
        Type::BOOL | Type::INT2 | Type::INT4 | Type::INT8 => int::decode(py, ty, raw),
        Type::OID => numeric::decode_oid(py, raw),
        Type::FLOAT4 | Type::FLOAT8 => float::decode(py, ty, raw),
        Type::NUMERIC => numeric::decode(py, raw),
        Type::TEXT | Type::VARCHAR => text::decode(py, ty, raw),
        // void arrives as an empty, non-NULL value
        Type::VOID => Ok(py.None().into_bound(py)),
        _ => Err(decode_error(
            py,
            &format!("no decoder for the postgres type {}", ty.name()),
        )),
    }
}

fn encode_one(ty: &Type, param: &PyParam) -> Result<Param, String> {
    if matches!(param, PyParam::Null) {
        return Ok(Param::null());
    }
    let accepted = TYPE_TABLE
        .iter()
        .find(|info| info.matcher == Matcher::Oid(ty.oid()))
        .is_some_and(|info| info.accepted_params.contains(&param.type_name()));
    if !accepted {
        return Err(format!(
            "a Python {} is not accepted for {}",
            param.type_name(),
            ty.name()
        ));
    }
    match *ty {
        Type::BOOL | Type::INT2 | Type::INT4 | Type::INT8 => {
            int::encode(ty, param).map(Param::binary)
        }
        Type::OID => numeric::encode_oid(param).map(Param::binary),
        Type::FLOAT4 | Type::FLOAT8 => float::encode(ty, param).map(Param::binary),
        // numeric travels as text so any Decimal or int keeps full precision
        Type::NUMERIC => numeric::encode(param).map(Param::text),
        Type::TEXT | Type::VARCHAR => text::encode(ty, param).map(Param::binary),
        _ => Err(format!("no encoder for the postgres type {}", ty.name())),
    }
}

/// Encodes every parameter against the statement's parameter types; runs without the GIL.
///
/// # Errors
/// `Interface` when the count differs from the statement's, `Encode` naming `$n` otherwise.
pub fn encode_all(params: &[PyParam], types: &[Type]) -> Result<Vec<Param>, Error> {
    if params.len() != types.len() {
        return Err(Error::Interface(format!(
            "the query takes {} parameters but {} were given",
            types.len(),
            params.len()
        )));
    }
    params
        .iter()
        .zip(types)
        .enumerate()
        .map(|(index, (param, ty))| {
            encode_one(ty, param).map_err(|msg| Error::Encode(format!("${}: {msg}", index + 1)))
        })
        .collect()
}

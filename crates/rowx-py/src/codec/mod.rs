pub mod bytes;
pub mod extract;
pub mod float;
pub mod int;
pub mod json;
pub mod numeric;
pub mod temporal;
pub mod temporal_enc;
pub mod text;
pub mod uuid;

use pyo3::prelude::*;
use rowx_pg::error::Error;
use rowx_pg::exec::Param;
use rowx_pg::types::{Matcher, TYPE_TABLE};
use rowx_pg::{Kind, Type};

pub use extract::{PyParam, extract};

use crate::errors::decode_error;

// a domain is transparent: it behaves as its base type, however deeply nested
fn resolve(ty: &Type) -> &Type {
    match ty.kind() {
        Kind::Domain(base) => resolve(base),
        _ => ty,
    }
}

fn is_citext(ty: &Type) -> bool {
    matches!(ty.kind(), Kind::Simple) && ty.name() == "citext"
}

/// One arm per type family; T17-T18 add the rest.
///
/// # Errors
/// `DecodeError` for an unsupported type or a malformed value.
pub fn decode<'py>(py: Python<'py>, ty: &Type, raw: Option<&[u8]>) -> PyResult<Bound<'py, PyAny>> {
    let Some(raw) = raw else {
        return Ok(py.None().into_bound(py));
    };
    let ty = resolve(ty);
    match *ty {
        Type::BOOL | Type::INT2 | Type::INT4 | Type::INT8 => int::decode(py, ty, raw),
        Type::OID => numeric::decode_oid(py, raw),
        Type::FLOAT4 | Type::FLOAT8 => float::decode(py, ty, raw),
        Type::NUMERIC => numeric::decode(py, raw),
        Type::TEXT | Type::VARCHAR | Type::BPCHAR | Type::NAME => text::decode(py, ty, raw),
        Type::CHAR => text::decode_char(py, raw),
        Type::BYTEA => Ok(bytes::decode(py, raw)),
        Type::UUID => uuid::decode(py, raw),
        Type::JSON | Type::JSONB => json::decode(py, ty, raw),
        Type::DATE => temporal::decode_date(py, raw),
        Type::TIME => temporal::decode_time(py, raw),
        Type::TIMETZ => temporal::decode_timetz(py, raw),
        Type::TIMESTAMP | Type::TIMESTAMPTZ => temporal::decode_timestamp(py, ty, raw),
        Type::INTERVAL => temporal::decode_interval(py, raw),
        _ if matches!(ty.kind(), Kind::Enum(_)) || is_citext(ty) => text::decode(py, ty, raw),
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
    let ty = resolve(ty);
    let is_enum = matches!(ty.kind(), Kind::Enum(_));
    let accepted = TYPE_TABLE
        .iter()
        .find(|info| match info.matcher {
            Matcher::Oid(oid) => oid == ty.oid(),
            Matcher::EnumKind => is_enum,
            Matcher::NamedSimple(name) => is_citext(ty) && name == ty.name(),
            _ => false,
        })
        // json takes any value; json.dumps is the arbiter
        .is_some_and(|info| {
            info.accepted_params.contains(&"object")
                || info.accepted_params.contains(&param.type_name())
        });
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
        Type::TEXT | Type::VARCHAR | Type::BPCHAR | Type::NAME => {
            text::encode(ty, param).map(Param::binary)
        }
        Type::CHAR => text::encode_char(param).map(Param::binary),
        Type::BYTEA => bytes::encode(param).map(Param::binary),
        Type::UUID => uuid::encode(param),
        Type::JSON | Type::JSONB => json::encode(param).map(Param::text),
        Type::DATE => temporal_enc::encode_date(param).map(Param::binary),
        Type::TIME => temporal_enc::encode_time(param).map(Param::binary),
        // timetz travels as text so the offset needs no sign flip
        Type::TIMETZ => temporal_enc::encode_timetz(param).map(Param::text),
        Type::TIMESTAMP => temporal_enc::encode_timestamp(param).map(Param::binary),
        Type::TIMESTAMPTZ => temporal_enc::encode_timestamptz(param).map(Param::binary),
        Type::INTERVAL => temporal_enc::encode_interval(param).map(Param::binary),
        // an enum label is sent as text so the server resolves it
        _ if is_enum => text::encode(ty, param).map(Param::text),
        _ if is_citext(ty) => text::encode(ty, param).map(Param::binary),
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

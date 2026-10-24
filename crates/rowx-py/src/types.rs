use pyo3::prelude::*;
use rowx_pg::types::{Codec, Format, Matcher, TYPE_TABLE, TzRule};

#[pyclass(
    frozen,
    get_all,
    skip_from_py_object,
    module = "rowx._core",
    name = "TypeInfo"
)]
#[derive(Debug, Clone)]
pub struct TypeInfo {
    key: String,
    // one of "oid", "enum", "named", "array", "domain"
    matcher: String,
    matcher_oid: Option<u32>,
    matcher_name: Option<String>,
    py_result: String,
    // one of "none", "aware", "naive"
    tz: String,
    accepted_params: Vec<String>,
    decode: String,
    encode: String,
    // one of "binary", "text"
    encode_format: String,
}

const fn codec_name(codec: Codec) -> &'static str {
    match codec {
        Codec::Unsupported => "unsupported",
    }
}

impl From<&rowx_pg::types::TypeInfo> for TypeInfo {
    fn from(t: &rowx_pg::types::TypeInfo) -> Self {
        let (matcher, matcher_oid, matcher_name) = match t.matcher {
            Matcher::Oid(oid) => ("oid", Some(oid), None),
            Matcher::EnumKind => ("enum", None, None),
            Matcher::NamedSimple(name) => ("named", None, Some(name.to_owned())),
            Matcher::ArrayKind => ("array", None, None),
            Matcher::DomainKind => ("domain", None, None),
        };
        Self {
            key: t.key.to_owned(),
            matcher: matcher.to_owned(),
            matcher_oid,
            matcher_name,
            py_result: t.py_result.to_owned(),
            tz: match t.tz {
                TzRule::None => "none",
                TzRule::Aware => "aware",
                TzRule::Naive => "naive",
            }
            .to_owned(),
            accepted_params: t.accepted_params.iter().map(|s| (*s).to_owned()).collect(),
            decode: codec_name(t.decode).to_owned(),
            encode: codec_name(t.encode).to_owned(),
            encode_format: match t.encode_format {
                Format::Binary => "binary",
                Format::Text => "text",
            }
            .to_owned(),
        }
    }
}

#[pyfunction]
pub fn type_table() -> Vec<TypeInfo> {
    TYPE_TABLE.iter().map(TypeInfo::from).collect()
}

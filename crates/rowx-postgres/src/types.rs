#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Matcher {
    Oid(u32),
    EnumKind,
    NamedSimple(&'static str),
    ArrayKind,
    DomainKind,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TzRule {
    None,
    Aware,
    Naive,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Format {
    Binary,
    Text,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Codec {
    Unsupported,
    Int,
    Float,
    Numeric,
    Text,
    Bytes,
    Uuid,
    Network,
    Mac,
    Json,
    Temporal,
    Array,
    Void,
}

#[derive(Debug, Clone, Copy)]
pub struct TypeInfo {
    pub key: &'static str,
    pub matcher: Matcher,
    pub py_result: &'static str,
    pub tz: TzRule,
    pub accepted_params: &'static [&'static str],
    pub decode: Codec,
    pub encode: Codec,
    pub encode_format: Format,
}

const fn row(
    key: &'static str,
    matcher: Matcher,
    py_result: &'static str,
    tz: TzRule,
    accepted_params: &'static [&'static str],
    encode_format: Format,
) -> TypeInfo {
    TypeInfo {
        key,
        matcher,
        py_result,
        tz,
        accepted_params,
        decode: Codec::Unsupported,
        encode: Codec::Unsupported,
        encode_format,
    }
}

impl TypeInfo {
    const fn codecs(mut self, decode: Codec, encode: Codec) -> Self {
        self.decode = decode;
        self.encode = encode;
        self
    }
}

const INT: &[&str] = &["int"];
const STR: &[&str] = &["str"];

pub static TYPE_TABLE: &[TypeInfo] = &[
    row(
        "bool",
        Matcher::Oid(16),
        "bool",
        TzRule::None,
        &["bool"],
        Format::Binary,
    )
    .codecs(Codec::Int, Codec::Int),
    row(
        "int2",
        Matcher::Oid(21),
        "int",
        TzRule::None,
        INT,
        Format::Binary,
    )
    .codecs(Codec::Int, Codec::Int),
    row(
        "int4",
        Matcher::Oid(23),
        "int",
        TzRule::None,
        INT,
        Format::Binary,
    )
    .codecs(Codec::Int, Codec::Int),
    row(
        "int8",
        Matcher::Oid(20),
        "int",
        TzRule::None,
        INT,
        Format::Binary,
    )
    .codecs(Codec::Int, Codec::Int),
    row(
        "oid",
        Matcher::Oid(26),
        "int",
        TzRule::None,
        INT,
        Format::Binary,
    )
    .codecs(Codec::Numeric, Codec::Numeric),
    row(
        "float4",
        Matcher::Oid(700),
        "float",
        TzRule::None,
        &["float", "int"],
        Format::Binary,
    )
    .codecs(Codec::Float, Codec::Float),
    row(
        "float8",
        Matcher::Oid(701),
        "float",
        TzRule::None,
        &["float", "int"],
        Format::Binary,
    )
    .codecs(Codec::Float, Codec::Float),
    row(
        "numeric",
        Matcher::Oid(1700),
        "decimal.Decimal",
        TzRule::None,
        &["Decimal", "int"],
        Format::Text,
    )
    .codecs(Codec::Numeric, Codec::Numeric),
    row(
        "text",
        Matcher::Oid(25),
        "str",
        TzRule::None,
        STR,
        Format::Binary,
    )
    .codecs(Codec::Text, Codec::Text),
    row(
        "varchar",
        Matcher::Oid(1043),
        "str",
        TzRule::None,
        STR,
        Format::Binary,
    )
    .codecs(Codec::Text, Codec::Text),
    row(
        "bpchar",
        Matcher::Oid(1042),
        "str",
        TzRule::None,
        STR,
        Format::Binary,
    )
    .codecs(Codec::Text, Codec::Text),
    row(
        "name",
        Matcher::Oid(19),
        "str",
        TzRule::None,
        STR,
        Format::Binary,
    )
    .codecs(Codec::Text, Codec::Text),
    row(
        "citext",
        Matcher::NamedSimple("citext"),
        "str",
        TzRule::None,
        STR,
        Format::Binary,
    )
    .codecs(Codec::Text, Codec::Text),
    row(
        "char",
        Matcher::Oid(18),
        "str",
        TzRule::None,
        STR,
        Format::Binary,
    )
    .codecs(Codec::Text, Codec::Text),
    row(
        "bytea",
        Matcher::Oid(17),
        "bytes",
        TzRule::None,
        &["bytes", "bytearray", "memoryview"],
        Format::Binary,
    )
    .codecs(Codec::Bytes, Codec::Bytes),
    row(
        "date",
        Matcher::Oid(1082),
        "datetime.date",
        TzRule::None,
        &["date"],
        Format::Binary,
    )
    .codecs(Codec::Temporal, Codec::Temporal),
    row(
        "time",
        Matcher::Oid(1083),
        "datetime.time",
        TzRule::Naive,
        &["time"],
        Format::Binary,
    )
    .codecs(Codec::Temporal, Codec::Temporal),
    row(
        "timetz",
        Matcher::Oid(1266),
        "datetime.time",
        TzRule::Aware,
        &["time"],
        Format::Text,
    )
    .codecs(Codec::Temporal, Codec::Temporal),
    row(
        "timestamp",
        Matcher::Oid(1114),
        "datetime.datetime",
        TzRule::Naive,
        &["datetime"],
        Format::Binary,
    )
    .codecs(Codec::Temporal, Codec::Temporal),
    row(
        "timestamptz",
        Matcher::Oid(1184),
        "datetime.datetime",
        TzRule::Aware,
        &["datetime"],
        Format::Binary,
    )
    .codecs(Codec::Temporal, Codec::Temporal),
    row(
        "interval",
        Matcher::Oid(1186),
        "datetime.timedelta",
        TzRule::None,
        &["timedelta"],
        Format::Binary,
    )
    .codecs(Codec::Temporal, Codec::Temporal),
    row(
        "uuid",
        Matcher::Oid(2950),
        "uuid.UUID",
        TzRule::None,
        &["UUID", "str"],
        Format::Binary,
    )
    .codecs(Codec::Uuid, Codec::Uuid),
    row(
        "inet",
        Matcher::Oid(869),
        "ipaddress.IPv4Interface | ipaddress.IPv6Interface",
        TzRule::None,
        &[
            "IPv4Interface",
            "IPv6Interface",
            "IPv4Address",
            "IPv6Address",
            "str",
        ],
        Format::Binary,
    )
    .codecs(Codec::Network, Codec::Network),
    row(
        "cidr",
        Matcher::Oid(650),
        "ipaddress.IPv4Network | ipaddress.IPv6Network",
        TzRule::None,
        &["IPv4Network", "IPv6Network", "str"],
        Format::Binary,
    )
    .codecs(Codec::Network, Codec::Network),
    row(
        "macaddr",
        Matcher::Oid(829),
        "str",
        TzRule::None,
        STR,
        Format::Text,
    )
    .codecs(Codec::Mac, Codec::Mac),
    row(
        "macaddr8",
        Matcher::Oid(774),
        "str",
        TzRule::None,
        STR,
        Format::Text,
    )
    .codecs(Codec::Mac, Codec::Mac),
    // json/jsonb results take the user's annotation (docs/design/checker.md, rules), so the leaf is `object`
    row(
        "json",
        Matcher::Oid(114),
        "object",
        TzRule::None,
        &["object"],
        Format::Text,
    )
    .codecs(Codec::Json, Codec::Json),
    row(
        "jsonb",
        Matcher::Oid(3802),
        "object",
        TzRule::None,
        &["object"],
        Format::Text,
    )
    .codecs(Codec::Json, Codec::Json),
    row(
        "enum",
        Matcher::EnumKind,
        "str",
        TzRule::None,
        STR,
        Format::Text,
    )
    .codecs(Codec::Text, Codec::Text),
    // a domain resolves to its base type at dispatch time, so it carries no metadata of its own
    row(
        "domain",
        Matcher::DomainKind,
        "",
        TzRule::None,
        &[],
        Format::Binary,
    ),
    row(
        "array",
        Matcher::ArrayKind,
        "list",
        TzRule::None,
        &["list", "tuple"],
        Format::Binary,
    )
    .codecs(Codec::Array, Codec::Array),
    row(
        "void",
        Matcher::Oid(2278),
        "None",
        TzRule::None,
        &[],
        Format::Binary,
    )
    .codecs(Codec::Void, Codec::Unsupported),
];

#[cfg(test)]
mod tests {
    use std::collections::HashSet;

    use super::{Matcher, TYPE_TABLE};

    #[test]
    fn keys_are_unique() {
        let keys: HashSet<_> = TYPE_TABLE.iter().map(|t| t.key).collect();
        assert_eq!(keys.len(), TYPE_TABLE.len());
    }

    #[test]
    fn matchers_are_unique() {
        for (i, a) in TYPE_TABLE.iter().enumerate() {
            for b in &TYPE_TABLE[i + 1..] {
                assert_ne!(a.matcher, b.matcher, "{} vs {}", a.key, b.key);
            }
        }
    }

    // the table has one generic array row; its element is resolved recursively at dispatch time
    #[test]
    fn array_row_is_a_single_sequence_row() {
        let arrays: Vec<_> = TYPE_TABLE
            .iter()
            .filter(|t| t.matcher == Matcher::ArrayKind)
            .collect();
        assert_eq!(arrays.len(), 1);
        assert_eq!(arrays[0].accepted_params, ["list", "tuple"]);
    }

    #[test]
    fn supported_rows_accept_params_except_void() {
        for t in TYPE_TABLE.iter().filter(|t| !t.py_result.is_empty()) {
            assert!(
                t.key == "void" || !t.accepted_params.is_empty(),
                "{} has no accepted params",
                t.key
            );
        }
    }

    #[test]
    fn supported_rows_have_codecs_except_void_encode() {
        for t in TYPE_TABLE.iter().filter(|t| !t.py_result.is_empty()) {
            assert_ne!(t.decode, super::Codec::Unsupported, "{} decode", t.key);
            assert!(
                t.key == "void" || t.encode != super::Codec::Unsupported,
                "{} encode",
                t.key
            );
        }
    }

    #[test]
    fn text_accepts_only_str() {
        let text = TYPE_TABLE.iter().find(|t| t.key == "text");
        assert_eq!(text.map(|t| t.accepted_params), Some(&["str"][..]));
    }
}

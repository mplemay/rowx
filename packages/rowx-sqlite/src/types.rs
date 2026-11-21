use pyo3::prelude::*;
use rowx_sqlite::types::Kind;

// one row per `rowx_sqlite::types::Kind`: its key, the Python type a column of it reads as, the
// zone rule of that type, the Python types whose natural binding is this kind, and the codec family
struct Row {
    kind: Kind,
    key: &'static str,
    py_result: &'static str,
    tz: &'static str,
    accepted_params: &'static [&'static str],
    codec: &'static str,
}

const TABLE: &[Row] = &[
    Row {
        kind: Kind::Integer,
        key: "integer",
        py_result: "int",
        tz: "none",
        accepted_params: &["int"],
        codec: "integer",
    },
    Row {
        kind: Kind::Real,
        key: "real",
        py_result: "float",
        tz: "none",
        accepted_params: &["float"],
        codec: "real",
    },
    Row {
        kind: Kind::Text,
        key: "text",
        py_result: "str",
        tz: "none",
        accepted_params: &["str"],
        codec: "text",
    },
    Row {
        kind: Kind::Blob,
        key: "blob",
        py_result: "bytes",
        tz: "none",
        accepted_params: &["bytes", "bytearray", "memoryview"],
        codec: "blob",
    },
    Row {
        kind: Kind::Boolean,
        key: "boolean",
        py_result: "bool",
        tz: "none",
        accepted_params: &["bool"],
        codec: "boolean",
    },
    Row {
        kind: Kind::DateTime,
        key: "datetime",
        py_result: "datetime.datetime",
        tz: "naive",
        accepted_params: &["datetime"],
        codec: "temporal",
    },
    Row {
        kind: Kind::DateTimeTz,
        key: "datetimetz",
        py_result: "datetime.datetime",
        tz: "aware",
        accepted_params: &["datetime"],
        codec: "temporal",
    },
    Row {
        kind: Kind::Date,
        key: "date",
        py_result: "datetime.date",
        tz: "none",
        accepted_params: &["date"],
        codec: "temporal",
    },
    Row {
        kind: Kind::Time,
        key: "time",
        py_result: "datetime.time",
        tz: "none",
        accepted_params: &["time"],
        codec: "temporal",
    },
    Row {
        kind: Kind::Decimal,
        key: "decimal",
        py_result: "decimal.Decimal",
        tz: "none",
        accepted_params: &["Decimal"],
        codec: "decimal",
    },
    Row {
        kind: Kind::Uuid,
        key: "uuid",
        py_result: "uuid.UUID",
        tz: "none",
        accepted_params: &["UUID"],
        codec: "uuid",
    },
    Row {
        kind: Kind::Json,
        key: "json",
        py_result: "object",
        tz: "none",
        accepted_params: &["dict", "list", "tuple"],
        codec: "json",
    },
    Row {
        kind: Kind::Any,
        key: "any",
        py_result: "int | float | str | bytes",
        tz: "none",
        accepted_params: &["int", "float", "str", "bytes"],
        codec: "any",
    },
];

/// The table key of a declared type's kind.
#[must_use]
pub(crate) fn key_of(kind: Kind) -> Option<&'static str> {
    TABLE.iter().find(|row| row.kind == kind).map(|row| row.key)
}

#[pyclass(
    frozen,
    get_all,
    skip_from_py_object,
    module = "rowx_sqlite._core",
    name = "TypeInfo"
)]
#[derive(Debug, Clone)]
pub struct TypeInfo {
    key: String,
    py_result: String,
    // one of "none", "aware", "naive"
    tz: String,
    accepted_params: Vec<String>,
    codec: String,
}

impl From<&Row> for TypeInfo {
    fn from(row: &Row) -> Self {
        Self {
            key: row.key.to_owned(),
            py_result: row.py_result.to_owned(),
            tz: row.tz.to_owned(),
            accepted_params: row
                .accepted_params
                .iter()
                .map(|name| (*name).to_owned())
                .collect(),
            codec: row.codec.to_owned(),
        }
    }
}

#[pyfunction]
#[must_use]
pub fn type_table() -> Vec<TypeInfo> {
    TABLE.iter().map(TypeInfo::from).collect()
}

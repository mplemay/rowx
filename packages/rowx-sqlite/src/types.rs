use pyo3::prelude::*;

// one row per `rowx_sqlite::types::Kind`: its key, the Python type a column of it reads as, the
// zone rule of that type, the Python types whose natural binding is this kind, and the codec family
struct Row {
    key: &'static str,
    py_result: &'static str,
    tz: &'static str,
    accepted_params: &'static [&'static str],
    codec: &'static str,
}

const TABLE: &[Row] = &[
    Row {
        key: "integer",
        py_result: "int",
        tz: "none",
        accepted_params: &["int"],
        codec: "integer",
    },
    Row {
        key: "real",
        py_result: "float",
        tz: "none",
        accepted_params: &["float"],
        codec: "real",
    },
    Row {
        key: "text",
        py_result: "str",
        tz: "none",
        accepted_params: &["str"],
        codec: "text",
    },
    Row {
        key: "blob",
        py_result: "bytes",
        tz: "none",
        accepted_params: &["bytes", "bytearray", "memoryview"],
        codec: "blob",
    },
    Row {
        key: "boolean",
        py_result: "bool",
        tz: "none",
        accepted_params: &["bool"],
        codec: "boolean",
    },
    Row {
        key: "datetime",
        py_result: "datetime.datetime",
        tz: "naive",
        accepted_params: &["datetime"],
        codec: "temporal",
    },
    Row {
        key: "datetimetz",
        py_result: "datetime.datetime",
        tz: "aware",
        accepted_params: &["datetime"],
        codec: "temporal",
    },
    Row {
        key: "date",
        py_result: "datetime.date",
        tz: "none",
        accepted_params: &["date"],
        codec: "temporal",
    },
    Row {
        key: "time",
        py_result: "datetime.time",
        tz: "none",
        accepted_params: &["time"],
        codec: "temporal",
    },
    Row {
        key: "decimal",
        py_result: "decimal.Decimal",
        tz: "none",
        accepted_params: &["Decimal"],
        codec: "decimal",
    },
    Row {
        key: "uuid",
        py_result: "uuid.UUID",
        tz: "none",
        accepted_params: &["UUID"],
        codec: "uuid",
    },
    Row {
        key: "json",
        py_result: "object",
        tz: "none",
        accepted_params: &["dict", "list", "tuple"],
        codec: "json",
    },
    Row {
        key: "any",
        py_result: "int | float | str | bytes",
        tz: "none",
        accepted_params: &["int", "float", "str", "bytes"],
        codec: "any",
    },
];

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

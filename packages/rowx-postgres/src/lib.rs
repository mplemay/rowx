pub mod admin;
pub mod codec;
pub mod conn;
pub mod describe;
pub mod errors;
pub mod exec;
pub mod migrate;
pub mod named;
pub mod pool;
pub mod stream;
pub mod types;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyList, PyString, PyTuple};
use pyo3::wrap_pyfunction;
use rowx_postgres::{Kind, Type};

#[pyfunction]
fn _cancel_requests_sent() -> u64 {
    rowx_postgres::cancel::requests_sent()
}

#[pyfunction]
fn _catalog_queries() -> u64 {
    rowx_postgres::describe::catalog_queries()
}

// test hook: the wire format ("binary" or "text") each parameter is sent in once encoded against the
// built-in types with these OIDs
#[pyfunction]
fn _param_formats(
    py: Python<'_>,
    oids: &Bound<'_, PyList>,
    params: &Bound<'_, PyList>,
) -> PyResult<Vec<&'static str>> {
    let types = oids
        .extract::<Vec<u32>>()?
        .into_iter()
        .map(|oid| {
            Type::from_oid(oid)
                .ok_or_else(|| PyValueError::new_err(format!("{oid} is not a built-in type")))
        })
        .collect::<PyResult<Vec<_>>>()?;
    let values = codec::extract(py, params)?;
    let encoded = codec::encode_all(&values, &types).map_err(|e| errors::to_py_err(py, &e))?;
    Ok(encoded
        .iter()
        .map(|param| if param.is_text() { "text" } else { "binary" })
        .collect())
}

// a type for the `_decode` hook: an int is a built-in oid, and a tuple names the rest, such as
// `("name", "hstore")`, `("enum",)`, `("array", spec)`, `("range", subtype_spec)` or `("domain", spec)`
const HOOK_MAX_DEPTH: usize = 16;

fn hook_type(spec: &Bound<'_, PyAny>, depth: usize) -> PyResult<Type> {
    if depth > HOOK_MAX_DEPTH {
        return Err(PyValueError::new_err(format!(
            "type spec nests deeper than {HOOK_MAX_DEPTH} levels"
        )));
    }
    if let Ok(oid) = spec.extract::<u32>() {
        return Type::from_oid(oid)
            .ok_or_else(|| PyValueError::new_err(format!("{oid} is not a built-in type")));
    }
    let tuple = spec.cast::<PyTuple>()?;
    let kind = tuple.get_item(0)?.cast_into::<PyString>()?.to_string();
    let new = |name: &str, kind: Kind| Type::new(name.to_owned(), 0, kind, "public".to_owned());
    match kind.as_str() {
        "name" => Ok(new(&tuple.get_item(1)?.extract::<String>()?, Kind::Simple)),
        "enum" => Ok(new("mood", Kind::Enum(vec!["ok".to_owned()]))),
        "array" => Ok(new(
            "_array",
            Kind::Array(hook_type(&tuple.get_item(1)?, depth + 1)?),
        )),
        "range" => Ok(new(
            "_range",
            Kind::Range(hook_type(&tuple.get_item(1)?, depth + 1)?),
        )),
        "domain" => Ok(new(
            "_domain",
            Kind::Domain(hook_type(&tuple.get_item(1)?, depth + 1)?),
        )),
        other => Err(PyValueError::new_err(format!("unknown type spec {other}"))),
    }
}

// test hook: decodes a binary value of the described type through the real codec, as a row would be
#[pyfunction]
fn _decode<'py>(
    py: Python<'py>,
    spec: &Bound<'py, PyAny>,
    raw: &Bound<'py, PyBytes>,
) -> PyResult<Bound<'py, PyAny>> {
    codec::decode(py, &hook_type(spec, 0)?, Some(raw.as_bytes()))
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<admin::DatabaseInfo>()?;
    module.add_function(wrap_pyfunction!(admin::database_exists, module)?)?;
    module.add_function(wrap_pyfunction!(admin::create_database, module)?)?;
    module.add_function(wrap_pyfunction!(admin::drop_database, module)?)?;
    module.add_function(wrap_pyfunction!(admin::list_databases, module)?)?;
    module.add_class::<conn::Connection>()?;
    module.add_function(wrap_pyfunction!(conn::connect, module)?)?;
    module.add_function(wrap_pyfunction!(conn::parse_dsn, module)?)?;
    module.add_class::<describe::ColumnInfo>()?;
    module.add_class::<describe::Describe>()?;
    module.add_class::<describe::ParamInfo>()?;
    module.add_function(wrap_pyfunction!(describe::connect_checker, module)?)?;
    module.add_function(wrap_pyfunction!(describe::describe, module)?)?;
    module.add_class::<migrate::Migrator>()?;
    module.add_class::<named::RowShape>()?;
    module.add_class::<named::ShapeIssue>()?;
    module.add_class::<pool::Pool>()?;
    module.add_class::<stream::RowStream>()?;
    module.add_class::<types::TypeInfo>()?;
    module.add_function(wrap_pyfunction!(named::map_columns_py, module)?)?;
    module.add_function(wrap_pyfunction!(types::type_table, module)?)?;
    module.add_function(wrap_pyfunction!(_cancel_requests_sent, module)?)?;
    module.add_function(wrap_pyfunction!(_catalog_queries, module)?)?;
    module.add_function(wrap_pyfunction!(_param_formats, module)?)?;
    module.add_function(wrap_pyfunction!(_decode, module)?)?;
    Ok(())
}

#[pymodule]
mod _core {
    use pyo3::prelude::*;

    #[pymodule_init]
    fn init(module: &Bound<'_, PyModule>) -> PyResult<()> {
        rowx_py_core::runtime::init()?;
        let py = module.py();
        rowx_py_core::errors::init(py)?;
        crate::register(module)
    }
}

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
fn hook_type(spec: &Bound<'_, PyAny>) -> PyResult<Type> {
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
        "array" => Ok(new("_array", Kind::Array(hook_type(&tuple.get_item(1)?)?))),
        "range" => Ok(new("_range", Kind::Range(hook_type(&tuple.get_item(1)?)?))),
        "domain" => Ok(new(
            "_domain",
            Kind::Domain(hook_type(&tuple.get_item(1)?)?),
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
    codec::decode(py, &hook_type(spec)?, Some(raw.as_bytes()))
}

/// Builds the `rowx._core.postgres` submodule.
///
/// # Errors
/// A Python error when a class or function cannot be registered.
pub fn module(py: Python<'_>) -> PyResult<Bound<'_, PyModule>> {
    let m = PyModule::new(py, "postgres")?;
    m.add_function(wrap_pyfunction!(admin::database_exists, &m)?)?;
    m.add_function(wrap_pyfunction!(admin::create_database, &m)?)?;
    m.add_function(wrap_pyfunction!(admin::drop_database, &m)?)?;
    m.add_function(wrap_pyfunction!(admin::list_databases, &m)?)?;
    m.add_class::<conn::Connection>()?;
    m.add_function(wrap_pyfunction!(conn::connect, &m)?)?;
    m.add_function(wrap_pyfunction!(conn::parse_dsn, &m)?)?;
    m.add_class::<describe::ColumnInfo>()?;
    m.add_class::<describe::Describe>()?;
    m.add_class::<describe::ParamInfo>()?;
    m.add_function(wrap_pyfunction!(describe::connect_checker, &m)?)?;
    m.add_function(wrap_pyfunction!(describe::describe, &m)?)?;
    m.add_class::<migrate::Migrator>()?;
    m.add_class::<named::RowShape>()?;
    m.add_class::<named::ShapeIssue>()?;
    m.add_class::<pool::Pool>()?;
    m.add_class::<stream::RowStream>()?;
    m.add_class::<types::TypeInfo>()?;
    m.add_function(wrap_pyfunction!(named::map_columns_py, &m)?)?;
    m.add_function(wrap_pyfunction!(types::type_table, &m)?)?;
    m.add_function(wrap_pyfunction!(_cancel_requests_sent, &m)?)?;
    m.add_function(wrap_pyfunction!(_catalog_queries, &m)?)?;
    m.add_function(wrap_pyfunction!(_param_formats, &m)?)?;
    m.add_function(wrap_pyfunction!(_decode, &m)?)?;
    Ok(m)
}

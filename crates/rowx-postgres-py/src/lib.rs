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
use pyo3::types::PyList;
use pyo3::wrap_pyfunction;
use rowx_postgres::Type;

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
    Ok(m)
}

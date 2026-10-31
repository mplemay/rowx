pub mod admin;
pub mod codec;
pub mod conn;
pub mod describe;
pub mod errors;
pub mod exec;
pub mod pool;
pub mod stream;
pub mod types;

use pyo3::prelude::*;
use pyo3::wrap_pyfunction;

#[pyfunction]
fn _cancel_requests_sent() -> u64 {
    rowx_postgres::cancel::requests_sent()
}

#[pyfunction]
fn _catalog_queries() -> u64 {
    rowx_postgres::describe::catalog_queries()
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
    m.add_class::<conn::Connection>()?;
    m.add_function(wrap_pyfunction!(conn::connect, &m)?)?;
    m.add_function(wrap_pyfunction!(conn::parse_dsn, &m)?)?;
    m.add_class::<describe::ColumnInfo>()?;
    m.add_class::<describe::Describe>()?;
    m.add_class::<describe::ParamInfo>()?;
    m.add_function(wrap_pyfunction!(describe::connect_checker, &m)?)?;
    m.add_function(wrap_pyfunction!(describe::describe, &m)?)?;
    m.add_class::<pool::Pool>()?;
    m.add_class::<stream::RowStream>()?;
    m.add_class::<types::TypeInfo>()?;
    m.add_function(wrap_pyfunction!(types::type_table, &m)?)?;
    m.add_function(wrap_pyfunction!(_cancel_requests_sent, &m)?)?;
    m.add_function(wrap_pyfunction!(_catalog_queries, &m)?)?;
    Ok(m)
}

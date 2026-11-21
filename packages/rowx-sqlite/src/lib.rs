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

use pyo3::prelude::*;
use pyo3::wrap_pyfunction;

#[pyfunction]
fn sqlite_version() -> &'static str {
    rowx_sqlite::sqlite_version()
}

// the runtime is bounded because SQLite work is synchronous and does not need the host's full parallelism
const MAX_WORKER_THREADS: usize = 4;

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<conn::Connection>()?;
    module.add_class::<pool::Pool>()?;
    module.add_class::<migrate::Migrator>()?;
    module.add_function(wrap_pyfunction!(conn::connect, module)?)?;
    module.add_function(wrap_pyfunction!(conn::parse_dsn, module)?)?;
    module.add_class::<named::RowShape>()?;
    module.add_class::<named::ShapeIssue>()?;
    module.add_class::<stream::RowStream>()?;
    module.add_class::<types::TypeInfo>()?;
    module.add_class::<describe::ColumnInfo>()?;
    module.add_class::<describe::Describe>()?;
    module.add_class::<describe::ParamInfo>()?;
    module.add_function(wrap_pyfunction!(describe::describe, module)?)?;
    module.add_function(wrap_pyfunction!(describe::connect_checker, module)?)?;
    module.add_function(wrap_pyfunction!(named::map_columns_py, module)?)?;
    module.add_function(wrap_pyfunction!(types::type_table, module)?)?;
    module.add_function(wrap_pyfunction!(sqlite_version, module)?)?;
    Ok(())
}

#[pymodule(name = "_core")]
mod core_module {
    use pyo3::prelude::*;

    #[pymodule_init]
    fn init(module: &Bound<'_, PyModule>) -> PyResult<()> {
        let workers = std::thread::available_parallelism()
            .map_or(1, std::num::NonZeroUsize::get)
            .clamp(1, super::MAX_WORKER_THREADS);
        rowx_py_core::runtime::init(Some(workers))?;
        rowx_py_core::errors::init(module.py())?;
        crate::register(module)
    }
}

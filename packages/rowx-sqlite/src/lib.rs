use pyo3::prelude::*;

#[pyfunction]
fn sqlite_version() -> &'static str {
    rowx_sqlite::sqlite_version()
}

// the runtime is bounded because SQLite work is synchronous and does not need the host's full parallelism
const MAX_WORKER_THREADS: usize = 4;

#[pymodule(name = "_core")]
mod core_module {
    use pyo3::prelude::*;

    #[pymodule_export]
    use super::sqlite_version;

    #[pymodule_init]
    fn init(module: &Bound<'_, PyModule>) -> PyResult<()> {
        let workers = std::thread::available_parallelism()
            .map_or(1, std::num::NonZeroUsize::get)
            .clamp(1, super::MAX_WORKER_THREADS);
        rowx_py_core::runtime::init(Some(workers))?;
        rowx_py_core::errors::init(module.py())
    }
}

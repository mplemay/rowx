use pyo3::prelude::*;

#[pymodule]
mod _core {
    use pyo3::prelude::*;

    #[pymodule_init]
    fn init(module: &Bound<'_, PyModule>) -> PyResult<()> {
        rowx_py_core::runtime::init()?;
        let py = module.py();
        rowx_py_core::errors::init(py)?;
        let postgres = rowx_postgres_py::module(py)?;
        module.add_submodule(&postgres)?;
        // a submodule is only an attribute until it is in sys.modules, which `from … import` needs
        py.import("sys")?
            .getattr("modules")?
            .set_item("rowx._core.postgres", &postgres)?;
        Ok(())
    }
}

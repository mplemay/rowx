pub mod codec;
pub mod conn;
pub mod describe;
pub mod errors;
pub mod exec;
pub mod runtime;
pub mod types;

use pyo3::prelude::*;

#[pymodule]
mod _core {
    use pyo3::prelude::*;

    #[pymodule_export]
    use crate::conn::{Connection, connect};
    #[pymodule_export]
    use crate::describe::{ColumnInfo, Describe, ParamInfo, connect_checker, describe};
    #[pymodule_export]
    use crate::types::{TypeInfo, type_table};

    #[pymodule_init]
    fn init(_module: &Bound<'_, PyModule>) -> PyResult<()> {
        crate::runtime::init()?;
        Python::attach(crate::errors::init)
    }

    #[pyfunction]
    fn _cancel_requests_sent() -> u64 {
        rowx_pg::cancel::requests_sent()
    }

    #[pyfunction]
    fn _catalog_queries() -> u64 {
        rowx_pg::describe::catalog_queries()
    }

    #[pyfunction]
    const fn ping() -> &'static str {
        "pong"
    }
}

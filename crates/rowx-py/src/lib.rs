pub mod types;

use pyo3::prelude::*;

#[pymodule]
mod _core {
    use pyo3::prelude::*;

    #[pymodule_export]
    use crate::types::{TypeInfo, type_table};

    #[pyfunction]
    const fn ping() -> &'static str {
        "pong"
    }
}

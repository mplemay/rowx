use pyo3::prelude::*;

#[pymodule]
mod _core {
    use pyo3::prelude::*;

    #[pyfunction]
    const fn ping() -> &'static str {
        "pong"
    }
}

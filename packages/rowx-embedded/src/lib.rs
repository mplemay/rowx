pub mod errors;
pub mod instance;

use pyo3::prelude::*;
use pyo3::types::PyTuple;
use rowx_embedded_host::{runtime, snapshot};

use crate::errors::{DirectoryInUseError, EngineError, Error, SnapshotError};
use crate::instance::{Instance, close_all};

#[pymodule]
fn _engine(py: Python<'_>, module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<Instance>()?;
    module.add_function(wrap_pyfunction!(close_all, module)?)?;
    module.add("EngineError", py.get_type::<EngineError>())?;
    module.add("DirectoryInUseError", py.get_type::<DirectoryInUseError>())?;
    module.add("SnapshotError", py.get_type::<SnapshotError>())?;
    module.add("ENGINE_VERSION", runtime::ENGINE_VERSION)?;
    module.add("RUNTIME_VERSION", runtime::runtime_version())?;
    module.add("WASM_SHA256", runtime::wasm_sha256().map_err(Error::from)?)?;
    module.add("SNAPSHOT_FORMAT", snapshot::SNAPSHOT_FORMAT)?;
    module.add("EXTENSIONS", PyTuple::new(py, runtime::EXTENSIONS)?)?;
    Ok(())
}

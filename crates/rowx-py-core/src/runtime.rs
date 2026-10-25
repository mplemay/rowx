use std::sync::OnceLock;

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use tokio::runtime::{Builder, Handle, Runtime};

static RUNTIME: OnceLock<Runtime> = OnceLock::new();

/// Builds the shared runtime once and registers it with pyo3-async-runtimes.
///
/// # Errors
/// `RuntimeError` when the worker threads cannot be started.
pub fn init() -> PyResult<()> {
    if RUNTIME.get().is_some() {
        return Ok(());
    }
    let runtime = Builder::new_multi_thread()
        .enable_all()
        .thread_name("rowx-io")
        .build()
        .map_err(|e| PyRuntimeError::new_err(format!("failed to start the rowx runtime: {e}")))?;
    // losing a race to another initialiser is fine: the loser is dropped and both are equivalent
    let runtime = RUNTIME.get_or_init(|| runtime);
    // an Err only means pyo3-async-runtimes already holds a runtime, which then drives the futures
    let _ = pyo3_async_runtimes::tokio::init_with_runtime(runtime);
    Ok(())
}

/// # Errors
/// `RuntimeError` before `init` has run.
pub fn handle() -> PyResult<Handle> {
    RUNTIME
        .get()
        .map(|rt| rt.handle().clone())
        .ok_or_else(|| PyRuntimeError::new_err("the rowx runtime is not initialised"))
}

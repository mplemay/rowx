use std::path::Path;
use std::sync::{Arc, Mutex, Weak};

use pyo3::prelude::*;
use pyo3::types::PyBytes;
use rowx_embedded_host::instance::{Embedded, join_background_closes};

use crate::errors::Error;

// the shared state of one `Instance`: the Python object, the registry and the destructor all see the same core
#[derive(Debug)]
struct Core {
    embedded: Arc<Embedded>,
    dsn: String,
    directory: Option<String>,
    boot: &'static str,
}

impl Drop for Core {
    // runs under the GIL when the Python object dies: never wait for the engine thread here
    fn drop(&mut self) {
        if !self.embedded.is_closed() {
            Arc::clone(&self.embedded).close_in_background();
        }
    }
}

// instances that are still alive, so `atexit` can close what the program forgot
static LIVE: Mutex<Vec<Weak<Core>>> = Mutex::new(Vec::new());

fn register(core: &Arc<Core>) {
    if let Ok(mut live) = LIVE.lock() {
        live.retain(|weak| weak.strong_count() > 0);
        live.push(Arc::downgrade(core));
    }
}

/// Closes every live instance of this process (a no-op for instances inherited across a fork).
#[pyfunction]
pub fn close_all(py: Python<'_>) {
    let cores: Vec<Arc<Core>> = LIVE
        .lock()
        .map(|live| live.iter().filter_map(Weak::upgrade).collect())
        .unwrap_or_default();
    for core in cores {
        // best effort: the process is exiting
        let _ = py.detach(|| core.embedded.close());
    }
    // instances whose Python objects were already dropped are being closed in the background
    py.detach(join_background_closes);
}

#[pyclass(module = "rowx_embedded._engine", name = "Instance", frozen)]
#[derive(Debug)]
pub struct Instance {
    core: Arc<Core>,
}

#[pymethods]
impl Instance {
    /// Starts an instance (a memory database, or a persistent `directory`; optionally seeded from a `snapshot`).
    #[staticmethod]
    #[pyo3(signature = (directory=None, snapshot=None))]
    fn start(py: Python<'_>, directory: Option<&str>, snapshot: Option<&[u8]>) -> PyResult<Self> {
        let embedded = py
            .detach(|| Embedded::start(directory.map(Path::new), snapshot))
            .map_err(Error::from)?;
        let core = Arc::new(Core {
            dsn: embedded.dsn().to_owned(),
            directory: embedded.directory().map(|path| path.display().to_string()),
            boot: embedded.boot().as_str(),
            embedded: Arc::new(embedded),
        });
        register(&core);
        Ok(Self { core })
    }

    #[getter]
    fn dsn(&self) -> &str {
        &self.core.dsn
    }

    #[getter]
    fn directory(&self) -> Option<&str> {
        self.core.directory.as_deref()
    }

    // how the database was brought up: "fresh", "seed" (fast snapshot boot) or "restore" (directory fallback)
    #[getter]
    fn boot(&self) -> &str {
        self.core.boot
    }

    #[getter]
    fn closed(&self) -> bool {
        self.core.embedded.is_closed()
    }

    /// A self-describing snapshot of the live instance; it keeps running.
    fn snapshot<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        let embedded = Arc::clone(&self.core.embedded);
        let bytes = py.detach(|| embedded.snapshot()).map_err(Error::from)?;
        Ok(PyBytes::new(py, &bytes))
    }

    /// Stops the instance and removes its socket directory. Idempotent; a no-op in a forked child.
    fn close(&self, py: Python<'_>) -> PyResult<()> {
        // a second caller (another thread) waits here until the first has finished closing
        let embedded = Arc::clone(&self.core.embedded);
        py.detach(|| embedded.close()).map_err(Error::from)?;
        Ok(())
    }

    /// Stops the instance at once, without waiting for the engine thread (a running statement is left to die with the
    /// process). A persistent directory is refused unless `force`. Idempotent with `close`; a no-op in a forked child.
    #[pyo3(signature = (*, force=false))]
    fn abandon(&self, force: bool) -> PyResult<()> {
        self.core.embedded.abandon(force).map_err(Error::from)?;
        Ok(())
    }

    const fn __enter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    #[pyo3(signature = (*_exc))]
    fn __exit__(&self, py: Python<'_>, _exc: &Bound<'_, pyo3::types::PyTuple>) -> PyResult<()> {
        self.close(py)
    }

    fn __repr__(&self) -> String {
        format!(
            "Instance(dsn={:?}, directory={:?}, closed={})",
            self.core.dsn,
            self.core.directory,
            self.core.embedded.is_closed()
        )
    }
}

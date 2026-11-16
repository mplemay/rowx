use pyo3::create_exception;
use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use rowx_embedded_host::error::HostError;

create_exception!(
    rowx_embedded._engine,
    EngineError,
    PyException,
    "Any failure of the embedded engine host."
);
create_exception!(
    rowx_embedded._engine,
    DirectoryInUseError,
    EngineError,
    "A persistent database directory is owned by another instance; `pid` is the owner when known."
);
create_exception!(
    rowx_embedded._engine,
    SnapshotError,
    EngineError,
    "A snapshot is malformed or was taken by an incompatible build."
);

// The crate's own error type: every fallible call in this crate returns it, and `From<Error> for PyErr` is the only
// place that chooses a Python exception class.
#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error(transparent)]
    Host(#[from] HostError),
}

impl From<Error> for PyErr {
    fn from(error: Error) -> Self {
        let Error::Host(host) = error;
        let message = host.to_string();
        match host {
            HostError::Locked { directory, pid } => {
                let err = DirectoryInUseError::new_err(message);
                Python::attach(|py| {
                    let value = err.value(py);
                    let _ = value.setattr("pid", pid);
                    let _ = value.setattr("directory", directory.display().to_string());
                });
                err
            }
            HostError::Snapshot(_) => SnapshotError::new_err(message),
            HostError::Engine(_)
            | HostError::Io(_)
            | HostError::EngineGone
            | HostError::InvalidArgument(_)
            | HostError::ForkedProcess
            | HostError::Closed
            | HostError::Unusable(_) => EngineError::new_err(message),
        }
    }
}

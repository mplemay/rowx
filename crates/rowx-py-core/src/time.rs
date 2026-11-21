use std::time::Duration;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// # Errors
/// `ValueError` when `secs` is negative, not finite or too large.
pub fn seconds(name: &str, secs: f64) -> PyResult<Duration> {
    Duration::try_from_secs_f64(secs).map_err(|_| {
        PyValueError::new_err(format!("{name} must be a non-negative number of seconds"))
    })
}

/// # Errors
/// `ValueError` when `secs` is set and negative, not finite or too large.
pub fn optional_seconds(name: &str, secs: Option<f64>) -> PyResult<Option<Duration>> {
    secs.map(|secs| seconds(name, secs)).transpose()
}

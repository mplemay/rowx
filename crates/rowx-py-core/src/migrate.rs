use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::task::{Context, Poll};

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyList, PyTuple};
use rowx_core::migrate::{Migration, MigrationDetail, MigrationStatus, RevertTarget};

#[derive(Debug, Clone, Copy)]
pub enum Op {
    // the target, and whether it is a dry run
    Run(Option<i64>, bool),
    Revert(RevertTarget, bool),
    // the target, and whether it is a dry run
    Skip(Option<i64>, bool),
    Info,
}

const fn state_name(status: MigrationStatus) -> &'static str {
    match status {
        MigrationStatus::Applied => "applied",
        MigrationStatus::Pending => "pending",
        MigrationStatus::Modified => "modified",
        MigrationStatus::Missing => "missing",
        MigrationStatus::Dirty => "dirty",
    }
}

// Sets the flag when it is dropped unfinished, before its inner future (whose drop sends the cancel
// request) goes: a struct's `drop` runs before its fields are dropped. pyo3's own done callback, which
// drops the Rust future, is registered before `watch_flag`, so the callbacks alone can't order them.
#[derive(Debug)]
pub struct StopFirst<F> {
    pub flag: Arc<AtomicBool>,
    pub finished: bool,
    pub inner: Pin<Box<F>>,
}

impl<F: Future> Future for StopFirst<F> {
    type Output = F::Output;

    fn poll(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<F::Output> {
        let out = self.inner.as_mut().poll(cx);
        if out.is_ready() {
            self.finished = true;
        }
        out
    }
}

impl<F> Drop for StopFirst<F> {
    fn drop(&mut self) {
        if !self.finished {
            self.flag.store(true, Ordering::Release);
        }
    }
}

// `Info` rows are (version, description, state, installed_on in microseconds since the epoch,
// execution_time in ns, applied checksum). Every other operation returns (version, elapsed in ns),
// which only a real run or revert has: a dry run and a skip took no time.
/// # Errors
/// A Python error when a value cannot be converted.
pub fn to_list(py: Python<'_>, op: Op, details: Vec<MigrationDetail>) -> PyResult<Py<PyAny>> {
    let rows = details
        .into_iter()
        .map(|d| match op {
            Op::Info => PyTuple::new(
                py,
                [
                    d.info.version.into_pyobject(py)?.into_any(),
                    d.info
                        .description
                        .unwrap_or_default()
                        .into_pyobject(py)?
                        .into_any(),
                    state_name(d.info.status).into_pyobject(py)?.into_any(),
                    d.installed_on_micros.into_pyobject(py)?.into_any(),
                    d.execution_time.into_pyobject(py)?.into_any(),
                    d.applied_checksum
                        .as_deref()
                        .map(|bytes| PyBytes::new(py, bytes))
                        .into_pyobject(py)?
                        .into_any(),
                ],
            ),
            Op::Run(_, false) | Op::Revert(_, false) => PyTuple::new(
                py,
                [
                    d.info.version.into_pyobject(py)?.into_any(),
                    d.execution_time.into_pyobject(py)?.into_any(),
                ],
            ),
            Op::Run(_, true) | Op::Revert(_, true) | Op::Skip(..) => PyTuple::new(
                py,
                [
                    d.info.version.into_pyobject(py)?.into_any(),
                    py.None().into_bound(py),
                ],
            ),
        })
        .collect::<PyResult<Vec<_>>>()?;
    Ok(PyList::new(py, rows)?.into_any().unbind())
}

/// An unknown target is a bad argument, so it fails before any IO.
///
/// # Errors
/// `ValueError` when the target names no forward migration.
pub fn check_target(migrations: &[Migration], op: Op) -> PyResult<()> {
    let (Op::Run(Some(target), _)
    | Op::Skip(Some(target), _)
    | Op::Revert(RevertTarget::Above(target), _)) = op
    else {
        return Ok(());
    };
    if migrations
        .iter()
        .any(|m| m.version == target && !m.kind.is_down())
    {
        return Ok(());
    }
    Err(PyValueError::new_err(format!(
        "target migration {target} does not exist"
    )))
}

// (version, description, kind, path, down_path, checksum, down_checksum, no_transaction), one per
// version: a reversible pair is a single entry
/// # Errors
/// A Python error when a value cannot be converted.
pub fn migration_rows<'py>(
    py: Python<'py>,
    migrations: &[Migration],
) -> PyResult<Bound<'py, PyList>> {
    let rows = migrations
        .iter()
        .filter(|m| !m.kind.is_down())
        .map(|m| {
            let down = migrations
                .iter()
                .find(|d| d.version == m.version && d.kind.is_down());
            let kind = if down.is_some() {
                "reversible"
            } else {
                "simple"
            };
            PyTuple::new(
                py,
                [
                    m.version.into_pyobject(py)?.into_any(),
                    m.description.as_str().into_pyobject(py)?.into_any(),
                    kind.into_pyobject(py)?.into_any(),
                    m.path.as_path().into_pyobject(py)?.into_any(),
                    down.map(|d| d.path.as_path()).into_pyobject(py)?.into_any(),
                    PyBytes::new(py, &m.checksum).into_any(),
                    down.map(|d| PyBytes::new(py, &d.checksum))
                        .into_pyobject(py)?
                        .into_any(),
                    PyBool::new(py, m.no_transaction).to_owned().into_any(),
                ],
            )
        })
        .collect::<PyResult<Vec<_>>>()?;
    PyList::new(py, rows)
}

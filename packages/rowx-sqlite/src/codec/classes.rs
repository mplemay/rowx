use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::PyType;

/// A class of the standard library, imported once.
///
/// # Errors
/// An import or attribute error.
pub(super) fn class<'py>(
    py: Python<'py>,
    cell: &'static PyOnceLock<Py<PyType>>,
    module: &str,
    name: &str,
) -> PyResult<&'py Bound<'py, PyType>> {
    let cls = cell.get_or_try_init(py, || {
        Ok::<_, PyErr>(
            py.import(module)?
                .getattr(name)?
                .cast_into::<PyType>()?
                .unbind(),
        )
    })?;
    Ok(cls.bind(py))
}

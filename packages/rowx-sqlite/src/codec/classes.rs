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

/// An attribute read through `cls`'s own descriptor, so a subclass of `cls` that shadows it with a property or
/// another value cannot change what is bound. An instance of `cls` itself reads it directly.
///
/// # Errors
/// `cls` has no such attribute, or its descriptor fails.
pub(super) fn field<'py>(
    obj: &Bound<'py, PyAny>,
    cls: &Bound<'py, PyType>,
    name: &str,
) -> PyResult<Bound<'py, PyAny>> {
    if obj.get_type().is(cls) {
        return obj.getattr(name);
    }
    cls.getattr(name)?.call_method1("__get__", (obj,))
}

/// A method of `cls` called unbound on `obj`, so a subclass override is never consulted.
///
/// # Errors
/// `cls` has no such method, or the method fails.
pub(super) fn call_base<'py>(
    obj: &Bound<'py, PyAny>,
    cls: &Bound<'py, PyType>,
    name: &str,
) -> PyResult<Bound<'py, PyAny>> {
    if obj.get_type().is(cls) {
        return obj.call_method0(name);
    }
    cls.getattr(name)?.call1((obj,))
}

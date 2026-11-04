use pyo3::prelude::*;
use pyo3::types::{PyList, PyTuple, PyType};

// Reads `name` as `cls` defines it, never as a subclass of `obj` redefines it. An object of exactly `cls` has
// nothing to override, so it takes the plain lookup; any other goes through the base class's own descriptor.
/// # Errors
/// The attribute lookup or the descriptor call failed.
pub fn field<'py>(
    obj: &Bound<'py, PyAny>,
    cls: &Bound<'py, PyType>,
    name: &str,
) -> PyResult<Bound<'py, PyAny>> {
    if obj.get_type().is(cls) {
        return obj.getattr(name);
    }
    cls.getattr(name)?.call_method1("__get__", (obj,))
}

// The items of a tuple or list read by index, so a subclass's `__iter__` is not consulted; any other iterable is
// iterated as it is.
/// # Errors
/// The value is not iterable.
pub fn items<'py>(obj: &Bound<'py, PyAny>) -> PyResult<Vec<Bound<'py, PyAny>>> {
    if let Ok(tuple) = obj.cast::<PyTuple>() {
        return Ok(tuple.iter().collect());
    }
    if let Ok(list) = obj.cast::<PyList>() {
        return Ok(list.iter().collect());
    }
    obj.try_iter()?.collect()
}

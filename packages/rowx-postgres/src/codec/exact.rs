use pyo3::prelude::*;
use pyo3::types::{PyList, PyTuple, PyType};

// Whether `obj` is a `cls`, by its real type. `isinstance` would also trust a `__class__` property, which a
// descriptor call then rejects.
/// # Errors
/// The subclass check failed.
pub fn is_a(obj: &Bound<'_, PyAny>, cls: &Bound<'_, PyType>) -> PyResult<bool> {
    obj.get_type().is_subclass(cls)
}

// The instance `__dict__` of an object whose real class `cls` defines one, read through `cls`'s own descriptor so a
// subclass property named `__dict__` or a data descriptor shadowing a key does not change it.
/// # Errors
/// `cls` has no `__dict__` descriptor, or `obj` is not an instance of it.
pub fn instance_dict<'py>(
    obj: &Bound<'py, PyAny>,
    cls: &Bound<'py, PyType>,
) -> PyResult<Bound<'py, PyAny>> {
    cls.getattr("__dict__")?
        .get_item("__dict__")?
        .call_method1("__get__", (obj,))
}

// `field` reads through the class's descriptor, which is a slot for the slotted value classes (`Range`, the
// geometric types and so on), so an instance `__dict__` entry cannot stand in for one; keep those classes slotted.
//
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

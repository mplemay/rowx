use pyo3::prelude::*;
use rowx_core::row_shape::Mapping;
use rowx_py_core::declare_row_classes;
use rowx_py_core::row::Layout;

declare_row_classes!("rowx_sqlite._core");

/// The layout of a named row, with the keys of a validator form taken from the result's column names.
///
/// # Errors
/// `InterfaceError` when the mapping does not fit the result or the row type.
pub fn layout_named(
    py: Python<'_>,
    shape: &RowShape,
    mapping: &Mapping,
    columns: &[String],
) -> PyResult<Layout> {
    let names: Vec<&str> = columns.iter().map(String::as_str).collect();
    Layout::named(py, shape.data(), mapping, &names)
}

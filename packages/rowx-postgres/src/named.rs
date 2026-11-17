use pyo3::prelude::*;
use rowx_core::row_shape::Mapping;
use rowx_postgres::{Column, Row};
use rowx_py_core::declare_row_classes;
use rowx_py_core::row::Layout;

declare_row_classes!("rowx_postgres._core");

/// The layout of a named row, with the keys of a validator form taken from the first row's columns.
///
/// # Errors
/// `InterfaceError` when the mapping does not fit the row or the row type.
pub fn layout_named(
    py: Python<'_>,
    shape: &RowShape,
    mapping: &Mapping,
    first: &Row,
) -> PyResult<Layout> {
    let names: Vec<&str> = first.columns().iter().map(Column::name).collect();
    Layout::named(py, shape.data(), mapping, &names)
}

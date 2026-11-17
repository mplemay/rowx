use pyo3::prelude::*;
use rowx_core::row_shape::Mapping;
use rowx_postgres::{Column, Row};
use rowx_py_core::declare_row_classes;
use rowx_py_core::row::Layout;
use rowx_py_core::shape::map_columns_data;

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

/// Maps result columns onto fields without a `RowShape`, for the checker.
///
/// Returns the field each column fills (`None` for a pass-through) and every violation. A by-name
/// row that breaks a rule still reports the columns that match exactly one field, so the checker
/// can keep judging their types; a positional row maps nothing once it has an error.
// pyo3 cannot extract a borrowed slice of `String`, so the list is taken by value
#[allow(clippy::needless_pass_by_value)]
#[pyfunction(name = "map_columns")]
#[must_use]
pub fn map_columns_py(
    columns: Vec<String>,
    fields: Vec<(Option<Vec<String>>, bool)>,
    positional: bool,
) -> (Vec<Option<usize>>, Vec<ShapeIssue>) {
    let (mapping, issues) = map_columns_data(&columns, fields, positional);
    (mapping, issues.into_iter().map(ShapeIssue).collect())
}

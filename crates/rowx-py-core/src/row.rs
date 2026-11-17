use std::sync::Arc;

use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString, PyTuple};
use rowx_core::row_shape::{Mapping, column_name};

use crate::errors::{interface_error, row_construct_error};
use crate::shape::{Form, RowShapeData};

/// Bit 0 of a column's flags: the column is nullable.
pub const NULLABLE: u8 = 1;
/// Bit 1 of a column's flags is for the backend to define (PostgreSQL reads an `interval` as
/// `rowx_postgres.Interval`). A backend names it where it reads it.
pub const BACKEND: u8 = 2;

/// How a row becomes Python.
#[derive(Debug, Default)]
pub enum RowForm {
    Scalar,
    #[default]
    Tuple,
    Named(Arc<RowShapeData>),
}

/// How a row becomes Python: a bare value, a tuple or a named row, and the flags of each column.
#[derive(Debug, Default)]
pub struct Layout {
    pub form: RowForm,
    // one flag byte per column; see `NULLABLE` and `BACKEND`
    pub flags: Vec<u8>,
    // a named row: the field each column fills (`None` passes through)
    pub targets: Vec<Option<usize>>,
    // a named row built by a validator: the key of each column
    pub keys: Vec<Py<PyString>>,
}

impl Layout {
    #[must_use]
    pub fn new(scalar: bool, flags: Option<&[u8]>) -> Self {
        Self {
            form: if scalar {
                RowForm::Scalar
            } else {
                RowForm::Tuple
            },
            flags: flags.unwrap_or_default().to_vec(),
            targets: Vec::new(),
            keys: Vec::new(),
        }
    }

    /// The layout of a named row: each column's target field, its flags from that field, and the
    /// validator form's keys from the raw column names.
    ///
    /// # Errors
    /// `InterfaceError` when the mapping does not fit the row or the row type.
    pub fn named(
        py: Python<'_>,
        shape: &Arc<RowShapeData>,
        mapping: &Mapping,
        names: &[&str],
    ) -> PyResult<Self> {
        if mapping.columns.len() != names.len()
            || mapping
                .columns
                .iter()
                .flatten()
                .any(|f| *f >= shape.keys.len())
        {
            return Err(interface_error(
                py,
                "the column mapping does not fit the result or the row type",
            ));
        }
        let flags = mapping
            .columns
            .iter()
            .map(|field| field.and_then(|f| shape.flags.get(f).copied()).unwrap_or(0))
            .collect();
        let keys = if matches!(shape.form, Form::Validator { .. }) {
            names
                .iter()
                .map(|name| PyString::intern(py, column_name(name).0).unbind())
                .collect()
        } else {
            Vec::new()
        };
        Ok(Self {
            form: RowForm::Named(Arc::clone(shape)),
            flags,
            targets: mapping.columns.clone(),
            keys,
        })
    }
}

/// The nullability half of the per-column flags.
#[must_use]
pub fn nullable_mask(flags: &[u8]) -> Vec<bool> {
    flags.iter().map(|flag| flag & NULLABLE != 0).collect()
}

/// One result row as Python, read through `cell`. `row_index` is the row's position in the returned
/// rows, counted across stream batches.
///
/// # Errors
/// Any error `cell` returns, and `RowConstructError` for a named row whose constructor raised.
pub fn decode_row<'py>(
    py: Python<'py>,
    columns: usize,
    mut cell: impl FnMut(usize) -> PyResult<Bound<'py, PyAny>>,
    layout: &Layout,
    row_index: usize,
) -> PyResult<Bound<'py, PyAny>> {
    match &layout.form {
        RowForm::Scalar => cell(0),
        RowForm::Tuple => {
            let cells = (0..columns).map(cell).collect::<PyResult<Vec<_>>>()?;
            Ok(PyTuple::new(py, cells)?.into_any())
        }
        RowForm::Named(shape) => construct(py, cell, layout, shape, row_index),
    }
}

/// How `layout` and `shape` build one named row from the cells `cell` reads.
///
/// # Errors
/// Any error `cell` returns, `RowConstructError` when the constructor or validator raises an
/// `Exception`, and anything else it raises as it is.
pub fn construct<'py>(
    py: Python<'py>,
    mut cell: impl FnMut(usize) -> PyResult<Bound<'py, PyAny>>,
    layout: &Layout,
    shape: &RowShapeData,
    row_index: usize,
) -> PyResult<Bound<'py, PyAny>> {
    let ctor = shape.ctor.bind(py);
    let built = match shape.form {
        Form::Positional => {
            let mut slots: Vec<Option<Bound<'py, PyAny>>> =
                (0..shape.keys.len()).map(|_| None).collect();
            for (index, target) in layout.targets.iter().enumerate() {
                if let Some(field) = target {
                    let Some(slot) = slots.get_mut(*field) else {
                        return Err(field_out_of_range(py));
                    };
                    *slot = Some(cell(index)?);
                }
            }
            // the cells fill a prefix of the fields, and the constructor fills the trailing defaults
            let mut args = Vec::with_capacity(slots.len());
            let mut gap = false;
            for slot in slots {
                match slot {
                    Some(value) if !gap => args.push(value),
                    Some(_) => {
                        return Err(interface_error(
                            py,
                            "a positional row cannot skip a field and fill a later one",
                        ));
                    }
                    None => gap = true,
                }
            }
            ctor.call1(PyTuple::new(py, args)?)
        }
        Form::Keyword => {
            let kwargs = fill(py, &mut cell, layout, |_, field| {
                field_key(py, shape, field)
            })?;
            ctor.call((), Some(&kwargs))
        }
        // the row is the dict itself
        Form::Dict => {
            return fill(py, &mut cell, layout, |_, field| {
                field_key(py, shape, field)
            })
            .map(Bound::into_any);
        }
        Form::Validator { validate } => {
            let dict = fill(py, &mut cell, layout, |index, _| {
                layout
                    .keys
                    .get(index)
                    .map(|k| Some(k.bind(py).clone()))
                    .ok_or_else(|| interface_error(py, "a column has no key in its row layout"))
            })?;
            if validate {
                ctor.call_method1("validate_python", (dict,))
            } else {
                ctor.call((), Some(&dict))
            }
        }
    };
    built.map_err(|err| wrap(py, err, shape, row_index))
}

// one pass over the cells; `key` names the entry of a column, or `None` to leave the column out
fn fill<'py>(
    py: Python<'py>,
    cell: &mut impl FnMut(usize) -> PyResult<Bound<'py, PyAny>>,
    layout: &Layout,
    key: impl Fn(usize, Option<usize>) -> PyResult<Option<Bound<'py, PyString>>>,
) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    for (index, target) in layout.targets.iter().enumerate() {
        if let Some(key) = key(index, *target)? {
            dict.set_item(key, cell(index)?)?;
        }
    }
    Ok(dict)
}

fn field_key<'py>(
    py: Python<'py>,
    shape: &RowShapeData,
    field: Option<usize>,
) -> PyResult<Option<Bound<'py, PyString>>> {
    field
        .map(|f| {
            shape
                .keys
                .get(f)
                .map(|k| k.bind(py).clone())
                .ok_or_else(|| field_out_of_range(py))
        })
        .transpose()
}

fn field_out_of_range(py: Python<'_>) -> PyErr {
    interface_error(py, "a column maps to a field its row type does not have")
}

// only an `Exception` is a construction failure; `CancelledError`, `KeyboardInterrupt` and the like
// are not the row's fault and pass through
fn wrap(py: Python<'_>, err: PyErr, shape: &RowShapeData, row_index: usize) -> PyErr {
    if err.is_instance_of::<PyException>(py) {
        construct_error(py, err, shape.type_name(), row_index)
    } else {
        err
    }
}

/// A named row whose constructor or validator raised `cause`, an `Exception`.
#[must_use]
pub fn construct_error(py: Python<'_>, cause: PyErr, type_name: &str, row_index: usize) -> PyErr {
    let detail = cause.value(py).repr().map_or_else(
        |_| "<unrepresentable>".to_owned(),
        |r| r.to_string_lossy().into_owned(),
    );
    row_construct_error(
        py,
        &format!("{type_name} row {row_index}: {detail}"),
        type_name,
        row_index,
        cause,
    )
}

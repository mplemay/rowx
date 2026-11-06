use pyo3::exceptions::{PyException, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString, PyTuple};
use rowx_core::row_shape::{
    FieldSpec, Mapping, Matching, Names, ShapeError, ShapeErrors, column_name, map_columns,
};
use rowx_postgres::Row;

use crate::errors::{construct_to_py_err, interface_error};
use crate::exec::{Layout, cell, nullable_mask};

/// How a named row is built from the cells of one result row.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Form {
    Positional,
    Keyword,
    Dict,
    // `validate` calls `ctor.validate_python(dict)`, otherwise the class is called with `**dict`
    Validator { validate: bool },
}

impl Form {
    const fn name(self) -> &'static str {
        match self {
            Self::Positional => "positional",
            Self::Keyword => "keyword",
            Self::Dict => "dict",
            Self::Validator { .. } => "validator",
        }
    }
}

const fn matching_name(matching: Matching) -> &'static str {
    match matching {
        Matching::ByName => "by_name",
        Matching::Positional => "positional",
    }
}

/// Everything the binding needs to map columns onto a named row type and build its rows. Python
/// builds it once per plan, so nothing here is computed per row.
#[pyclass(frozen, module = "rowx.postgres._core", name = "RowShape")]
#[derive(Debug)]
pub struct RowShape {
    ctor: Py<PyAny>,
    form: Form,
    matching: Matching,
    type_name: String,
    // the field names as interned strings (the dict and keyword keys) and as text (messages)
    keys: Vec<Py<PyString>>,
    key_names: Vec<String>,
    specs: Vec<FieldSpec>,
    flags: Vec<u8>,
    // bit 0 of `flags`; the one source of the nullability mask
    nullable: Vec<bool>,
}

const SPAN: u8 = 2;

impl RowShape {
    #[must_use]
    pub fn specs(&self) -> &[FieldSpec] {
        &self.specs
    }

    #[must_use]
    pub fn nullable(&self) -> &[bool] {
        &self.nullable
    }

    #[must_use]
    pub const fn matching(&self) -> Matching {
        self.matching
    }

    #[must_use]
    pub fn type_name(&self) -> &str {
        &self.type_name
    }

    #[must_use]
    pub fn key_names(&self) -> &[String] {
        &self.key_names
    }
}

#[pymethods]
impl RowShape {
    #[new]
    #[pyo3(signature = (ctor, form, matching, type_name, fields, /, *, validate = true))]
    fn new(
        py: Python<'_>,
        ctor: Py<PyAny>,
        form: &str,
        matching: &str,
        type_name: String,
        fields: Vec<(String, Option<Vec<String>>, bool, u8)>,
        validate: bool,
    ) -> PyResult<Self> {
        let form = match form {
            "positional" => Form::Positional,
            "keyword" => Form::Keyword,
            "dict" => Form::Dict,
            "validator" => Form::Validator { validate },
            other => return Err(PyValueError::new_err(format!("unknown row form {other:?}"))),
        };
        let matching = match matching {
            "by_name" => Matching::ByName,
            "positional" => Matching::Positional,
            other => {
                return Err(PyValueError::new_err(format!("unknown matching {other:?}")));
            }
        };
        let mut shape = Self {
            ctor,
            form,
            matching,
            type_name,
            keys: Vec::with_capacity(fields.len()),
            key_names: Vec::with_capacity(fields.len()),
            specs: Vec::with_capacity(fields.len()),
            flags: Vec::with_capacity(fields.len()),
            nullable: Vec::new(),
        };
        for (key, names, required, flag) in fields {
            shape.keys.push(PyString::intern(py, &key).unbind());
            shape.key_names.push(key);
            shape.specs.push(FieldSpec {
                names: names.map_or(Names::Unknown, Names::Known),
                required,
            });
            shape.flags.push(flag);
        }
        shape.nullable = nullable_mask(&shape.flags);
        Ok(shape)
    }

    #[getter]
    fn ctor(&self, py: Python<'_>) -> Py<PyAny> {
        self.ctor.clone_ref(py)
    }

    #[getter(form)]
    const fn form_name(&self) -> &'static str {
        self.form.name()
    }

    #[getter(matching)]
    const fn matching_text(&self) -> &'static str {
        matching_name(self.matching)
    }

    #[getter(type_name)]
    fn type_name_text(&self) -> &str {
        &self.type_name
    }
}

/// How `layout` and `shape` build one named row from `row`.
///
/// # Errors
/// `DecodeError` for a cell that cannot be decoded, `RowConstructError` when the constructor or
/// validator raises an `Exception`, and anything else it raises as it is.
pub fn construct<'py>(
    py: Python<'py>,
    row: &Row,
    layout: &Layout,
    shape: &RowShape,
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
                    *slot = Some(cell(py, row, index, layout)?);
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
            let kwargs = fill(py, row, layout, |_, field| field_key(py, shape, field))?;
            ctor.call((), Some(&kwargs))
        }
        // the row is the dict itself
        Form::Dict => {
            return fill(py, row, layout, |_, field| field_key(py, shape, field))
                .map(Bound::into_any);
        }
        Form::Validator { validate } => {
            let dict = fill(py, row, layout, |index, _| {
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
    row: &Row,
    layout: &Layout,
    key: impl Fn(usize, Option<usize>) -> PyResult<Option<Bound<'py, PyString>>>,
) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    for (index, target) in layout.targets.iter().enumerate() {
        if let Some(key) = key(index, *target)? {
            dict.set_item(key, cell(py, row, index, layout)?)?;
        }
    }
    Ok(dict)
}

fn field_key<'py>(
    py: Python<'py>,
    shape: &RowShape,
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
fn wrap(py: Python<'_>, err: PyErr, shape: &RowShape, row_index: usize) -> PyErr {
    if err.is_instance_of::<PyException>(py) {
        construct_to_py_err(py, err, &shape.type_name, row_index)
    } else {
        err
    }
}

impl Layout {
    /// The layout of a named row: each column's target field, its `Interval` flag from that field,
    /// and the validator form's keys from the column names.
    ///
    /// # Errors
    /// `InterfaceError` when the mapping does not fit the row or the row type.
    pub fn named(
        py: Python<'_>,
        shape: &Py<RowShape>,
        mapping: &Mapping,
        first: &Row,
    ) -> PyResult<Self> {
        let row_type = shape.get();
        if mapping.columns.len() != first.len()
            || mapping
                .columns
                .iter()
                .flatten()
                .any(|f| *f >= row_type.keys.len())
        {
            return Err(interface_error(
                py,
                "the column mapping does not fit the result or the row type",
            ));
        }
        let spans = mapping
            .columns
            .iter()
            .map(|field| {
                field.is_some_and(|f| row_type.flags.get(f).is_some_and(|b| b & SPAN != 0))
            })
            .collect();
        let keys = if matches!(row_type.form, Form::Validator { .. }) {
            first
                .columns()
                .iter()
                .map(|column| PyString::intern(py, column_name(column.name()).0).unbind())
                .collect()
        } else {
            Vec::new()
        };
        Ok(Self {
            form: crate::exec::RowForm::Named(shape.clone_ref(py)),
            spans,
            targets: mapping.columns.clone(),
            keys,
        })
    }
}

/// The text of a mapping failure, one message per issue, in the documented order.
#[must_use]
pub fn render_shape(
    type_name: &str,
    keys: &[String],
    errors: &ShapeErrors,
    columns: &[String],
) -> String {
    let name = |index: usize| columns.get(index).map_or("", |raw| column_name(raw).0);
    let key = |field: usize| keys.get(field).map_or("", String::as_str);
    let messages: Vec<String> = errors
        .errors()
        .iter()
        .map(|error| match error {
            ShapeError::UnexpectedColumn { index, name } => {
                format!(
                    "column {} \"{name}\" matches no field of `{type_name}`",
                    index + 1
                )
            }
            ShapeError::MissingColumn { field } => {
                format!("field \"{}\" of `{type_name}` has no column", key(*field))
            }
            ShapeError::DuplicateColumn { field, indices } => {
                let first = indices.first().map_or("", |i| name(*i));
                if indices.iter().all(|i| name(*i) == first) {
                    let numbers = join(indices.iter().map(|i| (i + 1).to_string()));
                    let verb = if indices.len() == 2 { "both" } else { "all" };
                    if first == "?column?" {
                        format!("columns {numbers} are {verb} unnamed (`?column?`)")
                    } else {
                        format!("columns {numbers} are {verb} named \"{first}\"")
                    }
                } else {
                    let columns = join(
                        indices
                            .iter()
                            .map(|i| format!("{} \"{}\"", i + 1, name(*i))),
                    );
                    let target = field.map_or("", |f| key(f));
                    let verb = if indices.len() == 2 { "both" } else { "all" };
                    format!("columns {columns} {verb} map to field \"{target}\" of `{type_name}`")
                }
            }
            ShapeError::AmbiguousColumn {
                index,
                name,
                fields,
            } => {
                let fields = join(fields.iter().map(|f| format!("\"{}\"", key(*f))));
                format!(
                    "column {} \"{name}\" matches fields {fields} of `{type_name}`",
                    index + 1
                )
            }
            ShapeError::NameMismatch {
                position,
                field,
                column,
            } => format!(
                "column {} \"{}\" faces field \"{}\" of `{type_name}` (position {})",
                column + 1,
                name(*column),
                key(*field),
                position + 1
            ),
            ShapeError::Arity { min, max, actual } => {
                let takes = if min == max {
                    format!("exactly {min}")
                } else {
                    format!("between {min} and {max}")
                };
                format!("the query returns {actual} columns but `{type_name}` takes {takes}")
            }
        })
        .collect();
    format!("{type_name}: {}", messages.join("; "))
}

/// The text of a NULL in a mapped field that is not nullable.
#[must_use]
pub fn render_null(type_name: &str, key: &str, column: usize, name: &str, row: usize) -> String {
    format!(
        "{type_name}: NULL in column {} \"{}\" for non-nullable field \"{key}\" of `{type_name}` (row {row})",
        column + 1,
        column_name(name).0
    )
}

// "a", "a and b", "a, b and c"
fn join(items: impl Iterator<Item = String>) -> String {
    let items: Vec<String> = items.collect();
    match items.split_last() {
        None => String::new(),
        Some((last, [])) => last.clone(),
        Some((last, rest)) => format!("{} and {last}", rest.join(", ")),
    }
}

/// What the checker learns about one violation of the mapping.
#[pyclass(
    frozen,
    get_all,
    skip_from_py_object,
    module = "rowx.postgres._core",
    name = "ShapeIssue"
)]
#[derive(Debug, Clone)]
pub struct ShapeIssue {
    kind: &'static str,
    columns: Vec<usize>,
    fields: Vec<usize>,
    name: Option<String>,
    min: Option<usize>,
    max: Option<usize>,
    actual: Option<usize>,
}

impl ShapeIssue {
    const fn new(kind: &'static str) -> Self {
        Self {
            kind,
            columns: Vec::new(),
            fields: Vec::new(),
            name: None,
            min: None,
            max: None,
            actual: None,
        }
    }

    fn of(error: &ShapeError, names: &[&str]) -> Self {
        let name = |index: usize| names.get(index).map(|n| (*n).to_owned());
        match error {
            ShapeError::UnexpectedColumn { index, name } => Self {
                columns: vec![*index],
                name: Some(name.clone()),
                ..Self::new("unexpected-column")
            },
            ShapeError::MissingColumn { field } => Self {
                fields: vec![*field],
                ..Self::new("missing-column")
            },
            ShapeError::DuplicateColumn { field, indices } => {
                let head = indices.first().and_then(|i| names.get(*i));
                let same = indices.iter().all(|i| names.get(*i) == head);
                Self {
                    columns: indices.clone(),
                    fields: field.iter().copied().collect(),
                    // two accepted names of one field have no single column name
                    name: if same {
                        indices.first().and_then(|i| name(*i))
                    } else {
                        None
                    },
                    ..Self::new("duplicate-column")
                }
            }
            ShapeError::AmbiguousColumn {
                index,
                name,
                fields,
            } => Self {
                columns: vec![*index],
                fields: fields.clone(),
                name: Some(name.clone()),
                ..Self::new("ambiguous-column")
            },
            ShapeError::NameMismatch { field, column, .. } => Self {
                columns: vec![*column],
                fields: vec![*field],
                name: name(*column),
                ..Self::new("column-name-mismatch")
            },
            ShapeError::Arity { min, max, actual } => Self {
                min: Some(*min),
                max: Some(*max),
                actual: Some(*actual),
                ..Self::new("arity-mismatch")
            },
        }
    }
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
    let specs: Vec<FieldSpec> = fields
        .into_iter()
        .map(|(names, required)| FieldSpec {
            names: names.map_or(Names::Unknown, Names::Known),
            required,
        })
        .collect();
    let matching = if positional {
        Matching::Positional
    } else {
        Matching::ByName
    };
    let raw: Vec<&str> = columns.iter().map(String::as_str).collect();
    match map_columns(&raw, &specs, matching) {
        Ok(mapping) => (mapping.columns, Vec::new()),
        Err(errors) => {
            let names: Vec<&str> = raw.iter().map(|r| column_name(r).0).collect();
            let issues = errors
                .errors()
                .iter()
                .map(|e| ShapeIssue::of(e, &names))
                .collect();
            let clean = if positional {
                vec![None; names.len()]
            } else {
                names.iter().map(|n| sole_field(n, &specs)).collect()
            };
            (clean, issues)
        }
    }
}

// the one field that accepts `name`
fn sole_field(name: &str, specs: &[FieldSpec]) -> Option<usize> {
    let mut hits = specs
        .iter()
        .enumerate()
        .filter(|(_, spec)| match &spec.names {
            Names::Known(known) => known.iter().any(|k| k == name),
            Names::Unknown => false,
        });
    match (hits.next(), hits.next()) {
        (Some((field, _)), None) => Some(field),
        _ => None,
    }
}

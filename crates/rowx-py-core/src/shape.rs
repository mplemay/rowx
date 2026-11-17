use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyString;
use rowx_core::row_shape::{
    FieldSpec, Matching, Names, ShapeError, ShapeErrors, column_name, map_columns,
};

use crate::row::nullable_mask;

/// How a named row is built from the cells of one result row.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Form {
    Positional,
    Keyword,
    Dict,
    // `validate` calls `ctor.validate_python(dict)`, otherwise the class is called with `**dict`
    Validator { validate: bool },
}

impl Form {
    #[must_use]
    pub const fn name(self) -> &'static str {
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

/// One field of a named row type: its key, the column names it accepts (`None` when they cannot be
/// determined), whether a column must fill it, and its per-column flags.
pub type FieldInput = (String, Option<Vec<String>>, bool, u8);

/// Everything a binding needs to map columns onto a named row type and build its rows. Python
/// builds it once per plan, so nothing here is computed per row.
///
/// The `RowShape` pyclass of each extension wraps it, since a pyclass names its module at compile
/// time.
#[derive(Debug)]
pub struct RowShapeData {
    pub(crate) ctor: Py<PyAny>,
    pub(crate) form: Form,
    matching: Matching,
    type_name: String,
    // the field names as interned strings (the dict and keyword keys) and as text (messages)
    pub(crate) keys: Vec<Py<PyString>>,
    key_names: Vec<String>,
    specs: Vec<FieldSpec>,
    pub(crate) flags: Vec<u8>,
    // bit 0 of `flags`; the one source of the nullability mask
    nullable: Vec<bool>,
}

impl RowShapeData {
    /// # Errors
    /// `ValueError` for an unknown `form` or `matching`.
    pub fn new(
        py: Python<'_>,
        ctor: Py<PyAny>,
        form: &str,
        matching: &str,
        type_name: String,
        fields: Vec<FieldInput>,
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

    #[must_use]
    pub fn ctor(&self, py: Python<'_>) -> Py<PyAny> {
        self.ctor.clone_ref(py)
    }

    #[must_use]
    pub const fn form_name(&self) -> &'static str {
        self.form.name()
    }

    #[must_use]
    pub const fn matching_text(&self) -> &'static str {
        matching_name(self.matching)
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

/// What the checker learns about one violation of the mapping. The `ShapeIssue` pyclass of each
/// extension wraps it.
#[derive(Debug, Clone)]
pub struct ShapeIssueData {
    pub kind: &'static str,
    pub columns: Vec<usize>,
    pub fields: Vec<usize>,
    pub name: Option<String>,
    pub min: Option<usize>,
    pub max: Option<usize>,
    pub actual: Option<usize>,
}

impl ShapeIssueData {
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

/// Maps result columns onto fields without a row shape, for the checker.
///
/// Returns the field each column fills (`None` for a pass-through) and every violation. A by-name
/// row that breaks a rule still reports the columns that match exactly one field, so the checker
/// can keep judging their types; a positional row maps nothing once it has an error.
#[must_use]
pub fn map_columns_data(
    columns: &[String],
    fields: Vec<(Option<Vec<String>>, bool)>,
    positional: bool,
) -> (Vec<Option<usize>>, Vec<ShapeIssueData>) {
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
                .map(|e| ShapeIssueData::of(e, &names))
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

/// Declares the `RowShape` and `ShapeIssue` pyclasses of an extension over [`RowShapeData`] and
/// [`ShapeIssueData`]. A pyclass names its module as a literal, so each extension declares its own
/// thin wrappers.
///
/// The caller needs `pyo3` and `std::sync::Arc` in scope paths used below (`pyo3` as a dependency).
#[macro_export]
macro_rules! declare_row_classes {
    ($module:literal) => {
        /// Everything the binding needs to map columns onto a named row type and build its rows. Python
        /// builds it once per plan, so nothing here is computed per row.
        #[::pyo3::pyclass(frozen, module = $module, name = "RowShape")]
        #[derive(Debug)]
        pub struct RowShape(pub ::std::sync::Arc<$crate::shape::RowShapeData>);

        impl RowShape {
            #[must_use]
            pub const fn data(&self) -> &::std::sync::Arc<$crate::shape::RowShapeData> {
                &self.0
            }
        }

        #[::pyo3::pymethods]
        impl RowShape {
            #[new]
            #[pyo3(signature = (ctor, form, matching, type_name, fields, /, *, validate = true))]
            fn new(
                py: ::pyo3::Python<'_>,
                ctor: ::pyo3::Py<::pyo3::PyAny>,
                form: &str,
                matching: &str,
                type_name: String,
                fields: Vec<$crate::shape::FieldInput>,
                validate: bool,
            ) -> ::pyo3::PyResult<Self> {
                $crate::shape::RowShapeData::new(
                    py, ctor, form, matching, type_name, fields, validate,
                )
                .map(|data| Self(::std::sync::Arc::new(data)))
            }

            #[getter]
            fn ctor(&self, py: ::pyo3::Python<'_>) -> ::pyo3::Py<::pyo3::PyAny> {
                self.0.ctor(py)
            }

            #[getter(form)]
            fn form_name(&self) -> &'static str {
                self.0.form_name()
            }

            #[getter(matching)]
            fn matching_text(&self) -> &'static str {
                self.0.matching_text()
            }

            #[getter(type_name)]
            fn type_name_text(&self) -> &str {
                self.0.type_name()
            }
        }

        /// What the checker learns about one violation of the mapping.
        #[::pyo3::pyclass(frozen, module = $module, name = "ShapeIssue")]
        #[derive(Debug)]
        pub struct ShapeIssue(pub $crate::shape::ShapeIssueData);

        #[::pyo3::pymethods]
        impl ShapeIssue {
            #[getter]
            const fn kind(&self) -> &'static str {
                self.0.kind
            }

            #[getter]
            fn columns(&self) -> Vec<usize> {
                self.0.columns.clone()
            }

            #[getter]
            fn fields(&self) -> Vec<usize> {
                self.0.fields.clone()
            }

            #[getter]
            fn name(&self) -> Option<String> {
                self.0.name.clone()
            }

            #[getter]
            const fn min(&self) -> Option<usize> {
                self.0.min
            }

            #[getter]
            const fn max(&self) -> Option<usize> {
                self.0.max
            }

            #[getter]
            const fn actual(&self) -> Option<usize> {
                self.0.actual
            }
        }
    };
}

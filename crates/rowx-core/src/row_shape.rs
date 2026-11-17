use std::collections::BTreeMap;
use std::fmt;

use thiserror::Error;

/// The nullability override a column name carries.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Override {
    /// A trailing `!`: the column is not null.
    Bang,
    /// A trailing `?`: the column is nullable.
    Question,
}

/// Splits a raw column name into the name it is matched under and its override.
///
/// One trailing `!` or `?` is stripped, so `id!!` becomes `id!`. The server's name for an unnamed
/// expression, `?column?`, is not an override.
#[must_use]
pub fn column_name(raw: &str) -> (&str, Option<Override>) {
    if raw == "?column?" {
        return (raw, None);
    }
    if let Some(rest) = raw.strip_suffix('!') {
        return (rest, Some(Override::Bang));
    }
    if let Some(rest) = raw.strip_suffix('?') {
        return (rest, Some(Override::Question));
    }
    (raw, None)
}

/// The column names a field accepts.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Names {
    Known(Vec<String>),
    /// The names cannot be determined, which turns the by-name rules off for the whole row type.
    Unknown,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FieldSpec {
    pub names: Names,
    /// `false` when the constructor fills the field in (a default, or a `NotRequired` key).
    pub required: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Matching {
    #[default]
    ByName,
    Positional,
}

/// A violation of the column-to-field mapping. Columns are 0-based indices into the query's
/// columns and fields are indices into the `fields` slice; the caller renders the text.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum ShapeError {
    #[error("column {index} ({name:?}) matches no field")]
    UnexpectedColumn { index: usize, name: String },
    #[error("field {field} has no column")]
    MissingColumn { field: usize },
    /// `field` is `None` when no field has the name (`?column?`).
    #[error("columns {indices:?} map to the same name or field (field {field:?})")]
    DuplicateColumn {
        field: Option<usize>,
        indices: Vec<usize>,
    },
    #[error("column {index} ({name:?}) matches fields {fields:?}")]
    AmbiguousColumn {
        index: usize,
        name: String,
        fields: Vec<usize>,
    },
    #[error("the query returns {actual} columns but the row takes between {min} and {max}")]
    Arity {
        min: usize,
        max: usize,
        actual: usize,
    },
    #[error("column {column} faces field {field} (position {position}) under another name")]
    NameMismatch {
        position: usize,
        field: usize,
        column: usize,
    },
}

/// Every violation of one mapping, in the documented order. Never empty.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ShapeErrors(Vec<ShapeError>);

impl ShapeErrors {
    #[must_use]
    pub fn errors(&self) -> &[ShapeError] {
        &self.0
    }
}

impl IntoIterator for ShapeErrors {
    type Item = ShapeError;
    type IntoIter = std::vec::IntoIter<ShapeError>;

    fn into_iter(self) -> Self::IntoIter {
        self.0.into_iter()
    }
}

impl fmt::Display for ShapeErrors {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        for (i, err) in self.0.iter().enumerate() {
            if i > 0 {
                f.write_str("; ")?;
            }
            write!(f, "{err}")?;
        }
        Ok(())
    }
}

impl std::error::Error for ShapeErrors {}

/// Where each column goes. `columns[i]` is the field column `i` fills, or `None` for a column
/// that passes through; `absent` lists the fields no column fills.
///
/// When any field's names are unknown the by-name checks are off, so two columns can map to one
/// field; the binding keeps the last.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Mapping {
    pub columns: Vec<Option<usize>>,
    pub absent: Vec<usize>,
}

/// Maps the query's raw column names to `fields`, collecting every violation.
///
/// # Errors
/// [`ShapeErrors`] listing each violation, columns first (by column index), then missing fields.
pub fn map_columns(
    raw_columns: &[&str],
    fields: &[FieldSpec],
    matching: Matching,
) -> Result<Mapping, ShapeErrors> {
    let names: Vec<&str> = raw_columns.iter().map(|raw| column_name(raw).0).collect();
    let errors = match matching {
        Matching::ByName => by_name(&names, fields),
        Matching::Positional => positional(&names, fields),
    };
    if !errors.is_empty() {
        return Err(ShapeErrors(errors));
    }
    let columns: Vec<Option<usize>> = match matching {
        Matching::ByName => names.iter().map(|n| matches(n, fields).next()).collect(),
        Matching::Positional => (0..names.len()).map(Some).collect(),
    };
    let absent = (0..fields.len())
        .filter(|f| !columns.contains(&Some(*f)))
        .collect();
    Ok(Mapping { columns, absent })
}

// the fields whose accepted names contain `name`
fn matches<'a>(name: &'a str, fields: &'a [FieldSpec]) -> impl Iterator<Item = usize> + 'a {
    fields
        .iter()
        .enumerate()
        .filter(move |(_, f)| match &f.names {
            Names::Known(known) => known.iter().any(|k| k == name),
            Names::Unknown => false,
        })
        .map(|(i, _)| i)
}

// sort rank within one column index
const UNEXPECTED: u8 = 0;
const DUPLICATE: u8 = 1;
const AMBIGUOUS: u8 = 2;

fn by_name(names: &[&str], fields: &[FieldSpec]) -> Vec<ShapeError> {
    // an unknown name set could accept anything, so nothing can be judged
    if fields.iter().any(|f| f.names == Names::Unknown) {
        return Vec::new();
    }
    let hits: Vec<Vec<usize>> = names.iter().map(|n| matches(n, fields).collect()).collect();
    // (column index, rank, error)
    let mut found: Vec<(usize, u8, ShapeError)> = Vec::new();

    let mut by_text: BTreeMap<&str, Vec<usize>> = BTreeMap::new();
    for (i, name) in names.iter().enumerate() {
        by_text.entry(name).or_default().push(i);
    }
    // two differently named columns that one field accepts
    let mut field_groups: Vec<(usize, Vec<usize>)> = Vec::new();
    for field in 0..fields.len() {
        let columns: Vec<usize> = (0..names.len())
            .filter(|&i| hits[i].contains(&field))
            .collect();
        let distinct = columns.iter().any(|&i| names[i] != names[columns[0]]);
        if distinct {
            field_groups.push((field, columns));
        }
    }

    let mut duplicated = vec![false; names.len()];
    for indices in by_text.values().filter(|v| v.len() > 1) {
        for &i in indices {
            duplicated[i] = true;
        }
        // a repeated name inside a reported field group is part of that report
        if field_groups
            .iter()
            .any(|(_, columns)| indices.iter().all(|i| columns.contains(i)))
        {
            continue;
        }
        found.push((
            indices[1],
            DUPLICATE,
            ShapeError::DuplicateColumn {
                field: hits[indices[0]].first().copied(),
                indices: indices.clone(),
            },
        ));
    }
    for (field, columns) in field_groups {
        found.push((
            columns[1],
            DUPLICATE,
            ShapeError::DuplicateColumn {
                field: Some(field),
                indices: columns,
            },
        ));
    }

    for (i, name) in names.iter().enumerate() {
        match hits[i].len() {
            0 if !duplicated[i] => found.push((
                i,
                UNEXPECTED,
                ShapeError::UnexpectedColumn {
                    index: i,
                    name: (*name).to_owned(),
                },
            )),
            0 | 1 => {}
            _ => found.push((
                i,
                AMBIGUOUS,
                ShapeError::AmbiguousColumn {
                    index: i,
                    name: (*name).to_owned(),
                    fields: hits[i].clone(),
                },
            )),
        }
    }

    found.sort_by_key(|(column, rank, _)| (*column, *rank));
    let mut errors: Vec<ShapeError> = found.into_iter().map(|(_, _, err)| err).collect();
    errors.extend(
        fields
            .iter()
            .enumerate()
            .filter(|(i, f)| f.required && !hits.iter().any(|h| h.contains(i)))
            .map(|(field, _)| ShapeError::MissingColumn { field }),
    );
    errors
}

fn positional(names: &[&str], fields: &[FieldSpec]) -> Vec<ShapeError> {
    let (min, max, actual) = (
        fields.iter().filter(|f| f.required).count(),
        fields.len(),
        names.len(),
    );
    if actual < min || actual > max {
        return vec![ShapeError::Arity { min, max, actual }];
    }
    names
        .iter()
        .zip(fields)
        .enumerate()
        .filter(|(_, (name, field))| match &field.names {
            Names::Known(known) => !known.iter().any(|k| k == *name),
            Names::Unknown => false,
        })
        .map(|(position, _)| ShapeError::NameMismatch {
            position,
            field: position,
            column: position,
        })
        .collect()
}

/// What the caller expects of the result, checked before and while rows stream in.
#[derive(Debug, Clone, Copy, Default)]
pub struct Shape<'a> {
    /// `true` marks a nullable column, so `false` rejects NULL there. For a named shape (`fields`
    /// set) it is per field instead, and the per-column mask comes from the mapping.
    pub mask: Option<&'a [bool]>,
    pub scalar: bool,
    pub limit_one: bool,
    pub arity: Option<usize>,
    /// The fields of a named row type; the columns are mapped onto them before execution.
    pub fields: Option<&'a [FieldSpec]>,
    pub matching: Matching,
}

/// A result whose columns do not fit the [`Shape`] the caller expects.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum ShapeCheckError {
    /// An arity, scalar or mask-length violation.
    #[error("{0}")]
    RowShape(String),
    /// A named row's columns do not map onto its fields; the binding renders the text from the
    /// indices and the raw column names.
    #[error("{errors}")]
    Mapping {
        errors: ShapeErrors,
        columns: Vec<String>,
    },
    /// The shape itself is malformed (a named shape without one nullability flag per field).
    #[error("{0}")]
    Interface(String),
}

/// A NULL in a column that is not nullable.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum NullError {
    #[error("NULL in a non-nullable column at index {index}")]
    Column { index: usize },
    /// A mapped column. The indices are 0-based, `name` is the raw column name, and `row` counts
    /// the rows delivered so far.
    #[error("NULL in non-nullable field {field} (column {column} {name:?}, row {row})")]
    Field {
        column: usize,
        field: usize,
        row: usize,
        name: Box<str>,
    },
}

/// Why a NULL check stopped: a NULL it found, or a cell it could not read.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum NullCheckError<E> {
    #[error(transparent)]
    Null(NullError),
    #[error("{0}")]
    Read(E),
}

fn missing_field_mask() -> ShapeCheckError {
    ShapeCheckError::Interface("a named shape needs one nullability flag per field".to_owned())
}

/// A named shape must carry a flag for every field, or NULL checks would silently switch off.
///
/// # Errors
/// `Interface` when the mask is missing or its length differs from the number of fields.
pub fn check_field_mask(
    fields: &[FieldSpec],
    mask: Option<&[bool]>,
) -> Result<(), ShapeCheckError> {
    match mask {
        Some(mask) if mask.len() == fields.len() => Ok(()),
        Some(mask) => Err(ShapeCheckError::Interface(format!(
            "the nullability mask covers {} fields, the named shape has {}",
            mask.len(),
            fields.len()
        ))),
        None => Err(missing_field_mask()),
    }
}

/// Checks `shape` against the result's raw column names and maps them onto the fields of a named
/// shape.
///
/// # Errors
/// `RowShape` for an arity, scalar or mask-length violation, `Mapping` for a mapping failure and
/// `Interface` for a malformed named shape.
pub fn check_shape(shape: &Shape<'_>, names: &[&str]) -> Result<Option<Mapping>, ShapeCheckError> {
    let count = names.len();
    if let Some(arity) = shape.arity
        && arity != count
    {
        return Err(ShapeCheckError::RowShape(format!(
            "expected {arity} columns, the query returns {count}"
        )));
    }
    if shape.scalar && count != 1 {
        return Err(ShapeCheckError::RowShape(format!(
            "a scalar result needs exactly 1 column, the query returns {count}"
        )));
    }
    // a named shape's mask is per field, so the mapping replaces the column length check
    if let Some(fields) = shape.fields {
        check_field_mask(fields, shape.mask)?;
        return map_columns(names, fields, shape.matching)
            .map(Some)
            .map_err(|errors| ShapeCheckError::Mapping {
                errors,
                columns: names.iter().map(|n| (*n).to_owned()).collect(),
            });
    }
    if let Some(mask) = shape.mask
        && mask.len() != count
    {
        return Err(ShapeCheckError::RowShape(format!(
            "the nullability mask covers {} columns, the query returns {count}",
            mask.len()
        )));
    }
    Ok(None)
}

/// The per-column mask of a named shape: a mapped column takes its field's flag, a pass-through
/// column is nullable. `check_shape` guarantees one flag per field, so a miss is a caller bug.
///
/// # Errors
/// `Interface` when the shape has no mask or the mask is shorter than a mapped field.
pub fn column_mask(shape: &Shape<'_>, mapping: &Mapping) -> Result<Vec<bool>, ShapeCheckError> {
    let fields = shape.mask.ok_or_else(missing_field_mask)?;
    mapping
        .columns
        .iter()
        .map(|field| {
            field.map_or(Ok(true), |f| {
                fields.get(f).copied().ok_or_else(missing_field_mask)
            })
        })
        .collect()
}

/// Rejects a NULL in a column whose `mask` entry is `false`. `is_null` reads one cell, which can
/// fail; `names` are the raw column names, and `row_index` counts the rows delivered so far.
///
/// # Errors
/// `Null` for the first NULL in a non-nullable column (a mapped column reports its field too),
/// `Read` when `is_null` fails.
pub fn check_nulls<E>(
    names: &[impl AsRef<str>],
    row_index: usize,
    mask: &[bool],
    mapping: Option<&Mapping>,
    mut is_null: impl FnMut(usize) -> Result<bool, E>,
) -> Result<(), NullCheckError<E>> {
    for (index, nullable) in mask.iter().enumerate() {
        if !nullable && is_null(index).map_err(NullCheckError::Read)? {
            // a named shape reports the field too, so the binding can render its message
            if let Some(field) = mapping.and_then(|m| m.columns.get(index).copied().flatten()) {
                return Err(NullCheckError::Null(NullError::Field {
                    column: index,
                    field,
                    row: row_index,
                    name: names
                        .get(index)
                        .map_or_else(Box::default, |n| n.as_ref().into()),
                }));
            }
            return Err(NullCheckError::Null(NullError::Column { index }));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{
        FieldSpec, Mapping, Matching, Names, NullCheckError, NullError, Override, Shape,
        ShapeCheckError, ShapeError, ShapeErrors, check_field_mask, check_nulls, check_shape,
        column_mask, column_name, map_columns,
    };

    fn field(names: &[&str], required: bool) -> FieldSpec {
        FieldSpec {
            names: Names::Known(names.iter().map(|n| (*n).to_owned()).collect()),
            required,
        }
    }

    fn req(name: &str) -> FieldSpec {
        field(&[name], true)
    }

    fn opt(name: &str) -> FieldSpec {
        field(&[name], false)
    }

    fn err(errors: Vec<ShapeError>) -> Result<Mapping, ShapeErrors> {
        Err(ShapeErrors(errors))
    }

    #[test]
    fn column_name_strips_one_override() {
        assert_eq!(column_name("id!"), ("id", Some(Override::Bang)));
        assert_eq!(column_name("id?"), ("id", Some(Override::Question)));
        assert_eq!(column_name("id!!"), ("id!", Some(Override::Bang)));
        assert_eq!(column_name("id"), ("id", None));
        assert_eq!(column_name("?column?"), ("?column?", None));
        assert_eq!(column_name("?"), ("", Some(Override::Question)));
    }

    #[test]
    fn by_name_ignores_column_order_and_strips_overrides() {
        let fields = [req("id"), req("name")];
        assert_eq!(
            map_columns(&["name?", "id!"], &fields, Matching::ByName),
            Ok(Mapping {
                columns: vec![Some(1), Some(0)],
                absent: vec![]
            })
        );
    }

    #[test]
    fn by_name_is_case_sensitive() {
        let fields = [req("id")];
        assert_eq!(
            map_columns(&["ID"], &fields, Matching::ByName),
            err(vec![
                ShapeError::UnexpectedColumn {
                    index: 0,
                    name: "ID".into()
                },
                ShapeError::MissingColumn { field: 0 },
            ])
        );
    }

    #[test]
    fn unexpected_column() {
        let fields = [req("id")];
        assert_eq!(
            map_columns(&["id", "age"], &fields, Matching::ByName),
            err(vec![ShapeError::UnexpectedColumn {
                index: 1,
                name: "age".into()
            }])
        );
    }

    #[test]
    fn missing_column_and_absent_default() {
        let fields = [req("id"), opt("name")];
        assert_eq!(
            map_columns(&["id"], &fields, Matching::ByName),
            Ok(Mapping {
                columns: vec![Some(0)],
                absent: vec![1]
            })
        );
        assert_eq!(
            map_columns(&["name"], &fields, Matching::ByName),
            err(vec![ShapeError::MissingColumn { field: 0 }])
        );
    }

    #[test]
    fn duplicate_column_with_a_field() {
        let fields = [req("id")];
        assert_eq!(
            map_columns(&["id", "id!", "id?"], &fields, Matching::ByName),
            err(vec![ShapeError::DuplicateColumn {
                field: Some(0),
                indices: vec![0, 1, 2]
            },])
        );
    }

    #[test]
    fn duplicate_column_without_a_field_is_not_also_unexpected() {
        let fields = [req("id")];
        assert_eq!(
            map_columns(&["?column?", "id", "?column?"], &fields, Matching::ByName),
            err(vec![ShapeError::DuplicateColumn {
                field: None,
                indices: vec![0, 2]
            }])
        );
    }

    #[test]
    fn duplicate_column_for_two_names_of_one_field() {
        let fields = [field(&["user_id", "id"], true)];
        let dup = ShapeError::DuplicateColumn {
            field: Some(0),
            indices: vec![0, 1],
        };
        assert_eq!(
            map_columns(&["id", "user_id"], &fields, Matching::ByName),
            err(vec![dup])
        );
        assert_eq!(
            map_columns(&["id"], &fields, Matching::ByName),
            Ok(Mapping {
                columns: vec![Some(0)],
                absent: vec![]
            })
        );
        assert_eq!(
            map_columns(&["user_id"], &fields, Matching::ByName),
            Ok(Mapping {
                columns: vec![Some(0)],
                absent: vec![]
            })
        );
    }

    #[test]
    fn ambiguous_column() {
        let fields = [field(&["y"], true), req("y")];
        assert_eq!(
            map_columns(&["y"], &fields, Matching::ByName),
            err(vec![ShapeError::AmbiguousColumn {
                index: 0,
                name: "y".into(),
                fields: vec![0, 1]
            }])
        );
    }

    #[test]
    fn repeated_name_inside_a_field_group_is_one_duplicate() {
        let fields = [field(&["id", "user_id"], true)];
        assert_eq!(
            map_columns(&["id", "user_id", "id"], &fields, Matching::ByName),
            err(vec![ShapeError::DuplicateColumn {
                field: Some(0),
                indices: vec![0, 1, 2]
            }])
        );
        let fields = [field(&["a", "b"], true)];
        assert_eq!(
            map_columns(&["a", "b", "a"], &fields, Matching::ByName),
            err(vec![ShapeError::DuplicateColumn {
                field: Some(0),
                indices: vec![0, 1, 2]
            }])
        );
    }

    #[test]
    fn duplicate_and_ambiguous_are_both_reported() {
        let fields = [req("y"), req("y")];
        assert_eq!(
            map_columns(&["y", "y"], &fields, Matching::ByName),
            err(vec![
                ShapeError::AmbiguousColumn {
                    index: 0,
                    name: "y".into(),
                    fields: vec![0, 1]
                },
                ShapeError::DuplicateColumn {
                    field: Some(0),
                    indices: vec![0, 1]
                },
                ShapeError::AmbiguousColumn {
                    index: 1,
                    name: "y".into(),
                    fields: vec![0, 1]
                },
            ])
        );
    }

    #[test]
    fn unknown_names_let_duplicate_columns_map_to_one_field() {
        let fields = [
            req("id"),
            FieldSpec {
                names: Names::Unknown,
                required: false,
            },
        ];
        assert_eq!(
            map_columns(&["id", "id", "zz"], &fields, Matching::ByName),
            Ok(Mapping {
                columns: vec![Some(0), Some(0), None],
                absent: vec![1]
            })
        );
    }

    #[test]
    fn unknown_names_pass_columns_through_and_suppress_the_rules() {
        let fields = [
            req("id"),
            FieldSpec {
                names: Names::Unknown,
                required: true,
            },
        ];
        assert_eq!(
            map_columns(&["extra", "extra", "other"], &fields, Matching::ByName),
            Ok(Mapping {
                columns: vec![None, None, None],
                absent: vec![0, 1]
            })
        );
        assert_eq!(
            map_columns(&["id", "x"], &fields, Matching::ByName),
            Ok(Mapping {
                columns: vec![Some(0), None],
                absent: vec![1]
            })
        );
    }

    #[test]
    fn positional_accepts_the_arity_range_with_trailing_defaults() {
        let fields = [req("id"), req("name"), opt("note")];
        assert_eq!(
            map_columns(&["id", "name"], &fields, Matching::Positional),
            Ok(Mapping {
                columns: vec![Some(0), Some(1)],
                absent: vec![2]
            })
        );
        assert_eq!(
            map_columns(&["id", "name", "note"], &fields, Matching::Positional),
            Ok(Mapping {
                columns: vec![Some(0), Some(1), Some(2)],
                absent: vec![]
            })
        );
    }

    #[test]
    fn positional_arity_mismatch_suppresses_name_errors() {
        let fields = [req("id"), req("name"), opt("note")];
        let arity = |actual| {
            err(vec![ShapeError::Arity {
                min: 2,
                max: 3,
                actual,
            }])
        };
        assert_eq!(
            map_columns(&["name"], &fields, Matching::Positional),
            arity(1)
        );
        assert_eq!(
            map_columns(&["a", "b", "c", "d"], &fields, Matching::Positional),
            arity(4)
        );
    }

    #[test]
    fn positional_name_mismatch_on_a_swapped_pair() {
        let fields = [req("id"), req("name")];
        assert_eq!(
            map_columns(&["name", "id"], &fields, Matching::Positional),
            err(vec![
                ShapeError::NameMismatch {
                    position: 0,
                    field: 0,
                    column: 0
                },
                ShapeError::NameMismatch {
                    position: 1,
                    field: 1,
                    column: 1
                },
            ])
        );
        assert_eq!(
            map_columns(&["id!", "name?"], &fields, Matching::Positional),
            Ok(Mapping {
                columns: vec![Some(0), Some(1)],
                absent: vec![]
            })
        );
    }

    #[test]
    fn mixed_errors_are_collected_in_order() {
        // fields: a, b (required), c (optional); y matches b and c
        let fields = [req("a"), field(&["b", "y"], true), field(&["y"], false)];
        assert_eq!(
            map_columns(
                &["zzz", "?column?", "y", "?column?", "zzz"],
                &fields,
                Matching::ByName
            ),
            err(vec![
                ShapeError::AmbiguousColumn {
                    index: 2,
                    name: "y".into(),
                    fields: vec![1, 2]
                },
                ShapeError::DuplicateColumn {
                    field: None,
                    indices: vec![1, 3]
                },
                ShapeError::DuplicateColumn {
                    field: None,
                    indices: vec![0, 4]
                },
                ShapeError::MissingColumn { field: 0 },
            ])
        );
    }

    #[test]
    fn errors_sort_by_the_second_column_of_a_duplicate() {
        let fields = [req("a")];
        assert_eq!(
            map_columns(&["q", "a", "q", "w"], &fields, Matching::ByName),
            err(vec![
                ShapeError::DuplicateColumn {
                    field: None,
                    indices: vec![0, 2]
                },
                ShapeError::UnexpectedColumn {
                    index: 3,
                    name: "w".into()
                },
            ])
        );
    }

    fn unknown_fields(count: usize) -> Vec<FieldSpec> {
        (0..count)
            .map(|_| FieldSpec {
                names: Names::Unknown,
                required: false,
            })
            .collect()
    }

    #[test]
    fn field_mask_must_cover_every_field() {
        assert_eq!(
            check_field_mask(&unknown_fields(2), Some(&[true, false])),
            Ok(())
        );
        assert!(matches!(
            check_field_mask(&unknown_fields(2), Some(&[true])),
            Err(ShapeCheckError::Interface(_))
        ));
        assert!(matches!(
            check_field_mask(&unknown_fields(2), Some(&[true, false, true])),
            Err(ShapeCheckError::Interface(_))
        ));
        assert!(matches!(
            check_field_mask(&unknown_fields(2), None),
            Err(ShapeCheckError::Interface(_))
        ));
    }

    #[test]
    fn field_mask_errors_keep_their_text() {
        assert_eq!(
            check_field_mask(&unknown_fields(2), Some(&[true])),
            Err(ShapeCheckError::Interface(
                "the nullability mask covers 1 fields, the named shape has 2".to_owned()
            ))
        );
        assert_eq!(
            check_field_mask(&unknown_fields(2), None)
                .map_err(|e| e.to_string())
                .err()
                .as_deref(),
            Some("a named shape needs one nullability flag per field")
        );
    }

    #[test]
    fn check_shape_accepts_a_plain_result() {
        let shape = Shape {
            arity: Some(2),
            mask: Some(&[true, false]),
            ..Shape::default()
        };
        assert_eq!(check_shape(&shape, &["a", "b"]), Ok(None));
        assert_eq!(check_shape(&Shape::default(), &["a", "b", "c"]), Ok(None));
    }

    #[test]
    fn check_shape_rejects_an_arity_mismatch() {
        let shape = Shape {
            arity: Some(3),
            ..Shape::default()
        };
        assert_eq!(
            check_shape(&shape, &["a", "b"]),
            Err(ShapeCheckError::RowShape(
                "expected 3 columns, the query returns 2".to_owned()
            ))
        );
    }

    #[test]
    fn check_shape_scalar_needs_exactly_one_column() {
        let shape = Shape {
            scalar: true,
            ..Shape::default()
        };
        assert_eq!(check_shape(&shape, &["a"]), Ok(None));
        for names in [&[][..], &["a", "b"][..]] {
            assert_eq!(
                check_shape(&shape, names),
                Err(ShapeCheckError::RowShape(format!(
                    "a scalar result needs exactly 1 column, the query returns {}",
                    names.len()
                )))
            );
        }
    }

    #[test]
    fn check_shape_rejects_a_mask_of_the_wrong_length() {
        let shape = Shape {
            mask: Some(&[true]),
            ..Shape::default()
        };
        assert_eq!(
            check_shape(&shape, &["a", "b"]),
            Err(ShapeCheckError::RowShape(
                "the nullability mask covers 1 columns, the query returns 2".to_owned()
            ))
        );
    }

    #[test]
    fn check_shape_maps_a_named_shape() {
        let fields = [req("a"), req("b")];
        let shape = Shape {
            mask: Some(&[true, false]),
            fields: Some(&fields),
            ..Shape::default()
        };
        assert_eq!(
            check_shape(&shape, &["b", "a"]),
            Ok(Some(Mapping {
                columns: vec![Some(1), Some(0)],
                absent: vec![]
            }))
        );
    }

    #[test]
    fn check_shape_reports_a_named_mapping_failure() {
        let fields = [req("a")];
        let shape = Shape {
            mask: Some(&[true]),
            fields: Some(&fields),
            ..Shape::default()
        };
        assert_eq!(
            check_shape(&shape, &["a", "extra"]),
            Err(ShapeCheckError::Mapping {
                errors: ShapeErrors(vec![ShapeError::UnexpectedColumn {
                    index: 1,
                    name: "extra".into()
                }]),
                columns: vec!["a".to_owned(), "extra".to_owned()],
            })
        );
    }

    #[test]
    fn check_shape_requires_a_field_mask_for_a_named_shape() {
        let fields = [req("a"), req("b")];
        for mask in [None, Some(&[true][..])] {
            let shape = Shape {
                mask,
                fields: Some(&fields),
                ..Shape::default()
            };
            assert!(matches!(
                check_shape(&shape, &["a", "b"]),
                Err(ShapeCheckError::Interface(_))
            ));
        }
    }

    #[test]
    fn column_mask_takes_each_columns_field_flag() {
        let mapping = Mapping {
            columns: vec![Some(1), None, Some(0)],
            absent: vec![],
        };
        let shape = Shape {
            mask: Some(&[false, true]),
            ..Shape::default()
        };
        // a pass-through column is nullable
        assert_eq!(column_mask(&shape, &mapping), Ok(vec![true, true, false]));
    }

    #[test]
    fn column_mask_without_a_flag_is_an_interface_error() {
        let mapping = Mapping {
            columns: vec![Some(1)],
            absent: vec![],
        };
        let short = Shape {
            mask: Some(&[true]),
            ..Shape::default()
        };
        assert!(matches!(
            column_mask(&short, &mapping),
            Err(ShapeCheckError::Interface(_))
        ));
        assert!(matches!(
            column_mask(&Shape::default(), &mapping),
            Err(ShapeCheckError::Interface(_))
        ));
    }

    fn nulls_at(nulls: &[usize]) -> impl FnMut(usize) -> Result<bool, String> + '_ {
        |index| Ok(nulls.contains(&index))
    }

    #[test]
    fn check_nulls_names_the_plain_column() {
        let err = check_nulls(&["a", "b"], 0, &[true, false], None, nulls_at(&[1]));
        assert_eq!(
            err,
            Err(NullCheckError::Null(NullError::Column { index: 1 }))
        );
        assert_eq!(
            NullError::Column { index: 1 }.to_string(),
            "NULL in a non-nullable column at index 1"
        );
    }

    #[test]
    fn check_nulls_names_the_mapped_field() {
        let mapping = Mapping {
            columns: vec![Some(2), Some(0)],
            absent: vec![],
        };
        let err = check_nulls(
            &["id", "email"],
            7,
            &[true, false],
            Some(&mapping),
            nulls_at(&[1]),
        );
        let expected = NullError::Field {
            column: 1,
            field: 0,
            row: 7,
            name: "email".into(),
        };
        assert_eq!(err, Err(NullCheckError::Null(expected.clone())));
        assert_eq!(
            expected.to_string(),
            "NULL in non-nullable field 0 (column 1 \"email\", row 7)"
        );
    }

    #[test]
    fn check_nulls_treats_a_pass_through_column_as_plain() {
        let mapping = Mapping {
            columns: vec![None],
            absent: vec![],
        };
        let err = check_nulls(&["x"], 0, &[false], Some(&mapping), nulls_at(&[0]));
        assert_eq!(
            err,
            Err(NullCheckError::Null(NullError::Column { index: 0 }))
        );
    }

    #[test]
    fn check_nulls_passes_nullable_columns_and_values() {
        assert_eq!(
            check_nulls(&["a", "b"], 0, &[true, true], None, nulls_at(&[0, 1])),
            Ok(())
        );
        assert_eq!(
            check_nulls(&["a", "b"], 0, &[false, false], None, nulls_at(&[])),
            Ok(())
        );
    }

    #[test]
    fn check_nulls_reads_only_non_nullable_cells() {
        let mut read = Vec::new();
        let result = check_nulls(&["a", "b", "c"], 0, &[true, false, false], None, |i| {
            read.push(i);
            Ok::<_, String>(false)
        });
        assert_eq!(result, Ok(()));
        assert_eq!(read, [1, 2]);
    }

    #[test]
    fn check_nulls_propagates_a_read_error() {
        let result = check_nulls(&["a", "b"], 0, &[false, false], None, |index| {
            if index == 0 {
                Ok(false)
            } else {
                Err("cell 1 is unreadable".to_owned())
            }
        });
        assert_eq!(
            result,
            Err(NullCheckError::Read("cell 1 is unreadable".to_owned()))
        );
    }
}

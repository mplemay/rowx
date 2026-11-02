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

#[cfg(test)]
mod tests {
    use super::{
        FieldSpec, Mapping, Matching, Names, Override, ShapeError, ShapeErrors, column_name,
        map_columns,
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
}

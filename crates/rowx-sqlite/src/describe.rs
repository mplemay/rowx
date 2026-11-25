use std::cell::OnceCell;
use std::collections::HashMap;

use rowx_core::row_shape::{Override, column_name};
use rusqlite::Connection as Raw;

use crate::bytecode::{Class, Fact, analyze};
use crate::error::Error;
use crate::exec::refresh_schema;
use crate::types::{Kind, lookup};

/// What the statement's text says about its shape. The lexer lives in Python, so the caller reads it
/// and hands the facts over; nothing here looks at the SQL text.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Structure {
    /// Table-like names in FROM and JOIN position and DML targets, each as its folded parts
    /// (`["schema", "name"]` or `["name"]`).
    pub sources: Vec<Vec<String>>,
    /// What the statement contains that keeps NOT NULL flags from being trusted.
    pub constructs: Vec<Construct>,
}

/// A construct after which a base column's NOT NULL flag proves nothing. Declared in the order the
/// cause is reported when a statement has several.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Construct {
    CommonTableExpression,
    Subquery,
    CompoundSelect,
    /// LEFT, RIGHT, FULL, OUTER and NATURAL joins.
    OuterJoin,
    /// An aggregate with no GROUP BY returns one row over an empty table, so every bare column is NULL then.
    Aggregate,
}

impl Construct {
    const fn cause(self) -> &'static str {
        match self {
            Self::CommonTableExpression => "common table expression",
            Self::Subquery => "subquery",
            Self::CompoundSelect => "compound select",
            Self::OuterJoin => "outer join",
            Self::Aggregate => "aggregate",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NullReason {
    NotNullColumn,
    /// The bytecode proves an expression is never NULL.
    ComputedExpr,
    NullableColumn,
    /// Nothing proves the column; `cause` says why.
    Unproven {
        cause: String,
    },
    OverrideBang,
    OverrideQuestion,
}

impl NullReason {
    #[must_use]
    pub const fn name(&self) -> &'static str {
        match self {
            Self::NotNullColumn => "NotNullColumn",
            Self::ComputedExpr => "ComputedExpr",
            Self::NullableColumn => "NullableColumn",
            Self::Unproven { .. } => "Unproven",
            Self::OverrideBang => "OverrideBang",
            Self::OverrideQuestion => "OverrideQuestion",
        }
    }

    /// The cause behind an unproven column.
    #[must_use]
    pub fn detail(&self) -> Option<&str> {
        match self {
            Self::Unproven { cause } => Some(cause),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParamInfo {
    /// `?1`, `:name`, `@name` or `$name`; `None` for a bare `?`.
    pub name: Option<String>,
}

/// The base column a result column comes from.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Origin {
    pub database: String,
    pub table: String,
    pub column: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ColumnInfo {
    /// The name with its `!` or `?` override stripped.
    pub name: String,
    pub raw_name: String,
    /// The declared type text, empty when there is none.
    pub type_name: String,
    /// `None` for a column without a declared type, which the checker treats as untyped.
    pub kind: Option<Kind>,
    pub nullable: bool,
    pub reason: NullReason,
    pub origin: Option<Origin>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Describe {
    pub params: Vec<ParamInfo>,
    pub columns: Vec<ColumnInfo>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum SourceKind {
    Table,
    View,
    Virtual,
    Missing,
}

// the schemas a bare name is looked up in, in SQLite's order; a qualified name is looked up in its own
fn schemas(parts: &[String]) -> Vec<&str> {
    match parts {
        [_] => vec!["temp", "main"],
        [schema, _] => vec![schema.as_str()],
        _ => Vec::new(),
    }
}

fn quote(name: &str) -> String {
    format!("\"{}\"", name.replace('"', "\"\""))
}

fn resolve(conn: &Raw, parts: &[String]) -> Result<SourceKind, Error> {
    let Some(name) = parts.last() else {
        return Ok(SourceKind::Missing);
    };
    for schema in schemas(parts) {
        // the whole schema is read and compared here: names fold by ASCII case, as SQLite folds them
        let sql = format!(
            "SELECT type, name, sql FROM {}.sqlite_schema",
            quote(schema)
        );
        let mut stmt = conn.prepare(&sql)?;
        let mut rows = stmt.query([])?;
        while let Some(row) = rows.next()? {
            let (kind, found, definition): (String, String, Option<String>) =
                (row.get(0)?, row.get(1)?, row.get(2)?);
            if !found.eq_ignore_ascii_case(name) {
                continue;
            }
            if kind == "view" {
                return Ok(SourceKind::View);
            }
            if kind != "table" {
                continue;
            }
            let is_virtual = definition.is_some_and(|text| {
                text.trim_start()
                    .get(..14)
                    .is_some_and(|head| head.eq_ignore_ascii_case("create virtual"))
            });
            return Ok(if is_virtual {
                SourceKind::Virtual
            } else {
                SourceKind::Table
            });
        }
    }
    Ok(SourceKind::Missing)
}

// Why base-column NOT NULL flags cannot be trusted for the statement, or `None` when they can: only a
// statement that reads ordinary tables through FROM and inner joins alone qualifies.
fn unproven_cause(structure: &Structure, kinds: &[SourceKind]) -> Option<String> {
    if let Some(construct) = structure.constructs.iter().min() {
        return Some(construct.cause().to_owned());
    }
    structure
        .sources
        .iter()
        .zip(kinds)
        .find_map(|(parts, kind)| {
            let name = parts.join(".");
            match kind {
                SourceKind::Table => None,
                SourceKind::View => Some(format!("view {name}")),
                SourceKind::Virtual | SourceKind::Missing => {
                    Some(format!("source {name} is not a table"))
                }
            }
        })
}

// the primary key columns of a table, and whether SQLite keeps a separate index for the key (which a
// rowid alias never has)
struct TableKey {
    columns: Vec<String>,
    names: Vec<String>,
    has_key_index: bool,
}

fn table_key(conn: &Raw, database: &str, table: &str) -> Result<TableKey, Error> {
    let mut columns = Vec::new();
    let mut names = Vec::new();
    // `table_xinfo` lists generated columns too; its columns are cid, name, type, notnull, dflt_value, pk, hidden
    conn.pragma(Some(database), "table_xinfo", table, |row| {
        let name: String = row.get(1)?;
        let pk: i64 = row.get(5)?;
        if pk > 0 {
            columns.push(name.clone());
        }
        names.push(name);
        Ok(())
    })?;
    // `index_list` columns are seq, name, unique, origin, partial
    let mut has_key_index = false;
    conn.pragma(Some(database), "index_list", table, |row| {
        let origin: String = row.get(3)?;
        has_key_index |= origin == "pk";
        Ok(())
    })?;
    Ok(TableKey {
        columns,
        names,
        has_key_index,
    })
}

const ROWID_NAMES: [&str; 3] = ["rowid", "oid", "_rowid_"];

// `INTEGER PRIMARY KEY` on a rowid table is the rowid, which is never NULL even though SQLite's NOT NULL
// flag is clear; so is the rowid itself when the column is named by one of its aliases
fn is_rowid(key: &TableKey, column: &str, declared: Option<&str>) -> bool {
    let named = |name: &String| name.eq_ignore_ascii_case(column);
    if ROWID_NAMES
        .iter()
        .any(|alias| alias.eq_ignore_ascii_case(column))
        && !key.names.iter().any(named)
    {
        return true;
    }
    key.columns.len() == 1
        && key.columns.iter().any(named)
        && !key.has_key_index
        && declared.is_some_and(|text| text.trim().eq_ignore_ascii_case("INTEGER"))
}

/// Describes one statement without running it. The statement is prepared and its result columns and
/// parameters are read; nothing is stepped.
///
/// # Errors
/// `Sqlite` (with the error offset) when SQLite rejects the SQL, `NotOneStatement` for no statement or
/// more than one.
pub fn describe(conn: &Raw, sql: &str, structure: &Structure) -> Result<Describe, Error> {
    refresh_schema(conn)?;
    let stmt = match conn.prepare(sql) {
        Ok(stmt) => stmt,
        Err(rusqlite::Error::MultipleStatement) => {
            return Err(Error::NotOneStatement(
                "describe takes exactly one statement, but the SQL has more".to_owned(),
            ));
        }
        Err(err) => return Err(err.into()),
    };
    // SQLite prepares nothing for text that holds only whitespace and comments
    if stmt.expanded_sql().is_none() {
        return Err(Error::NotOneStatement(
            "the SQL holds no statement".to_owned(),
        ));
    }
    let params = (1..=stmt.parameter_count())
        .map(|index| ParamInfo {
            name: stmt.parameter_name(index).map(str::to_owned),
        })
        .collect();
    let kinds = structure
        .sources
        .iter()
        .map(|parts| resolve(conn, parts))
        .collect::<Result<Vec<_>, _>>()?;
    let cause = unproven_cause(structure, &kinds);
    let mut keys: HashMap<(String, String), TableKey> = HashMap::new();
    // read only when a column needs it, and only from the bytecode, which is never run
    let facts: OnceCell<Option<Vec<Fact>>> = OnceCell::new();
    let fact = |index: usize| {
        facts
            .get_or_init(|| {
                (stmt.is_explain() == 0)
                    .then(|| analyze(conn, sql).ok())
                    .flatten()
                    .filter(|found| found.len() == stmt.column_count())
            })
            .as_ref()
            .and_then(|found| found.get(index))
            .copied()
    };
    let mut columns = Vec::with_capacity(stmt.column_count());
    let decls = stmt.columns();
    for (index, found) in stmt.columns_with_metadata().iter().enumerate() {
        let raw_name = found.name().to_owned();
        let declared = decls.get(index).and_then(|col| col.decl_type());
        let (name, over) = column_name(&raw_name);
        let name = name.to_owned();
        let origin = match (
            found.database_name(),
            found.table_name(),
            found.origin_name(),
        ) {
            (Some(database), Some(table), Some(column)) => Some(Origin {
                database: database.to_owned(),
                table: table.to_owned(),
                column: column.to_owned(),
            }),
            _ => None,
        };
        let proven = match (&cause, &origin) {
            (Some(cause), _) => Err(cause.clone()),
            (None, None) => Err("expression".to_owned()),
            (None, Some(origin)) => {
                // only now: a table-valued function's column has an origin that the schema does not know
                let (_, _, not_null, _, _) = conn.column_metadata(
                    Some(origin.database.as_str()),
                    origin.table.as_str(),
                    origin.column.as_str(),
                )?;
                let slot = (origin.database.clone(), origin.table.clone());
                if !keys.contains_key(&slot) {
                    let key = table_key(conn, &origin.database, &origin.table)?;
                    keys.insert(slot.clone(), key);
                }
                let rowid = keys
                    .get(&slot)
                    .is_some_and(|key| is_rowid(key, &origin.column, declared));
                Ok(not_null || rowid)
            }
        };
        let found = fact(index);
        let (nullable, reason) = verdict(over, proven, found);
        let (kind, type_name) = column_type(declared, found);
        columns.push(ColumnInfo {
            name,
            raw_name,
            type_name,
            kind,
            nullable,
            reason,
            origin,
        });
    }
    Ok(Describe { params, columns })
}

// The existing rule decides first, the bytecode only reads a column it left unproven, and an override applies last.
fn verdict(
    over: Option<Override>,
    proven: Result<bool, String>,
    found: Option<Fact>,
) -> (bool, NullReason) {
    match over {
        Some(Override::Bang) => (false, NullReason::OverrideBang),
        Some(Override::Question) => (true, NullReason::OverrideQuestion),
        None => match proven {
            Ok(true) => (false, NullReason::NotNullColumn),
            Ok(false) => (true, NullReason::NullableColumn),
            Err(cause) => match found {
                Some(fact) if fact.non_null => (
                    false,
                    if fact.pass_through {
                        NullReason::NotNullColumn
                    } else {
                        NullReason::ComputedExpr
                    },
                ),
                _ => (true, NullReason::Unproven { cause }),
            },
        },
    }
}

// A column with no declared type takes the one storage class the bytecode proves for it.
fn column_type(declared: Option<&str>, found: Option<Fact>) -> (Option<Kind>, String) {
    let looked_up = lookup(declared);
    if !looked_up.is_untyped() {
        return (
            Some(looked_up.kind()),
            declared.unwrap_or_default().to_owned(),
        );
    }
    found.and_then(|fact| fact.class).map_or_else(
        || (None, declared.unwrap_or_default().to_owned()),
        |class| (Some(class_kind(class)), class.name().to_owned()),
    )
}

const fn class_kind(class: Class) -> Kind {
    match class {
        Class::Integer => Kind::Integer,
        Class::Real => Kind::Real,
        Class::Text => Kind::Text,
        Class::Blob => Kind::Blob,
    }
}

#[cfg(test)]
mod tests {
    use super::{Construct, SourceKind, Structure, describe, unproven_cause};
    use crate::error::Error;
    use rusqlite::Connection;

    #[test]
    fn multiple_or_no_statements_are_not_one_statement() -> rusqlite::Result<()> {
        let conn = Connection::open_in_memory()?;
        for sql in ["select 1; select 2", "", "-- only a comment"] {
            let err = describe(&conn, sql, &Structure::default());
            assert!(
                matches!(err, Err(Error::NotOneStatement(_))),
                "{sql:?}: {err:?}"
            );
        }
        Ok(())
    }

    fn sources(names: &[&str]) -> Structure {
        Structure {
            sources: names.iter().map(|n| vec![(*n).to_owned()]).collect(),
            ..Structure::default()
        }
    }

    #[test]
    fn tables_alone_are_provable() {
        let structure = sources(&["a", "b"]);
        assert_eq!(
            unproven_cause(&structure, &[SourceKind::Table, SourceKind::Table]),
            None
        );
        assert_eq!(unproven_cause(&Structure::default(), &[]), None);
    }

    #[test]
    fn each_construct_makes_the_statement_unproven() {
        for (construct, cause) in [
            (Construct::CommonTableExpression, "common table expression"),
            (Construct::Subquery, "subquery"),
            (Construct::CompoundSelect, "compound select"),
            (Construct::OuterJoin, "outer join"),
            (Construct::Aggregate, "aggregate"),
        ] {
            let structure = Structure {
                constructs: vec![construct],
                ..Structure::default()
            };
            assert_eq!(unproven_cause(&structure, &[]).as_deref(), Some(cause));
        }
    }

    #[test]
    fn the_first_declared_construct_is_the_reported_cause() {
        let structure = Structure {
            constructs: vec![Construct::OuterJoin, Construct::Subquery],
            ..Structure::default()
        };
        assert_eq!(unproven_cause(&structure, &[]).as_deref(), Some("subquery"));
    }

    #[test]
    fn a_source_that_is_not_an_ordinary_table_makes_it_unproven() {
        let structure = sources(&["t", "v"]);
        assert_eq!(
            unproven_cause(&structure, &[SourceKind::Table, SourceKind::View]).as_deref(),
            Some("view v")
        );
        for kind in [SourceKind::Virtual, SourceKind::Missing] {
            assert_eq!(
                unproven_cause(&sources(&["x"]), &[kind]).as_deref(),
                Some("source x is not a table")
            );
        }
    }
}

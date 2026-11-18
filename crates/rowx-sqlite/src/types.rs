/// What a declared column type decodes to.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Integer,
    Real,
    Text,
    Blob,
    Boolean,
    /// A naive datetime.
    DateTime,
    /// An aware datetime, always UTC.
    DateTimeTz,
    Date,
    Time,
    Decimal,
    Uuid,
    Json,
    /// Decoded by the stored value's storage class (int, float, str or bytes).
    Any,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ColumnType {
    kind: Kind,
    untyped: bool,
}

impl ColumnType {
    #[must_use]
    pub const fn kind(self) -> Kind {
        self.kind
    }

    /// The column has no declared type (an expression, or a table column declared without one).
    #[must_use]
    pub const fn is_untyped(self) -> bool {
        self.untyped
    }

    /// An untyped column carries no NOT NULL guarantee a checker could rely on.
    #[must_use]
    pub const fn always_nullable(self) -> bool {
        self.untyped
    }

    const fn typed(kind: Kind) -> Self {
        Self {
            kind,
            untyped: false,
        }
    }
}

/// Looks up a column's type from its declared type (`sqlite3_column_decltype`).
///
/// Named conventions win over SQLite's affinity rules, which apply otherwise in SQLite's order
/// (`INT`, then `CHAR`/`CLOB`/`TEXT`, then `BLOB`, then `REAL`/`FLOA`/`DOUB`). Numeric affinity decodes
/// by storage class, as does a column without a declared type. Substring rules make `POINT` and
/// `INTERVAL` integer columns, the same trap SQLite itself has.
///
/// A STRICT table only has `INT`, `INTEGER`, `REAL`, `TEXT`, `BLOB` and `ANY` columns (`ANY` decodes by
/// storage class); any other declared type is not possible there and falls back to the rules above.
#[must_use]
pub fn lookup(declared: Option<&str>, strict: bool) -> ColumnType {
    let Some(declared) = declared.map(str::trim).filter(|text| !text.is_empty()) else {
        return ColumnType {
            kind: Kind::Any,
            untyped: true,
        };
    };
    let upper = declared.to_ascii_uppercase();
    if strict && let Some(kind) = strict_kind(&upper) {
        return ColumnType::typed(kind);
    }
    if let Some(kind) = named_kind(strip_arguments(&upper)) {
        return ColumnType::typed(kind);
    }
    ColumnType::typed(affinity_kind(&upper))
}

fn strict_kind(upper: &str) -> Option<Kind> {
    match upper {
        "INT" | "INTEGER" => Some(Kind::Integer),
        "REAL" => Some(Kind::Real),
        "TEXT" => Some(Kind::Text),
        "BLOB" => Some(Kind::Blob),
        "ANY" => Some(Kind::Any),
        _ => None,
    }
}

// `VARCHAR(255)` and `DECIMAL(10, 2)` name their type before the first parenthesis
fn strip_arguments(upper: &str) -> &str {
    match upper.split_once('(') {
        Some((name, rest)) if rest.trim_end().ends_with(')') => name.trim_end(),
        _ => upper,
    }
}

fn named_kind(name: &str) -> Option<Kind> {
    match name {
        "BOOLEAN" => Some(Kind::Boolean),
        "DATETIME" | "TIMESTAMP" => Some(Kind::DateTime),
        "TIMESTAMPTZ" | "DATETIMETZ" => Some(Kind::DateTimeTz),
        "DATE" => Some(Kind::Date),
        "TIME" => Some(Kind::Time),
        "DECIMAL" | "NUMERIC" => Some(Kind::Decimal),
        "UUID" => Some(Kind::Uuid),
        "JSON" => Some(Kind::Json),
        "INTEGER" => Some(Kind::Integer),
        "REAL" => Some(Kind::Real),
        "TEXT" => Some(Kind::Text),
        "BLOB" => Some(Kind::Blob),
        _ => None,
    }
}

// the five affinity rules of https://www.sqlite.org/datatype3.html#determination_of_column_affinity
fn affinity_kind(upper: &str) -> Kind {
    if upper.contains("INT") {
        Kind::Integer
    } else if ["CHAR", "CLOB", "TEXT"].iter().any(|s| upper.contains(s)) {
        Kind::Text
    } else if upper.contains("BLOB") {
        Kind::Blob
    } else if ["REAL", "FLOA", "DOUB"].iter().any(|s| upper.contains(s)) {
        Kind::Real
    } else {
        Kind::Any
    }
}

#[cfg(test)]
mod tests {
    use super::{Kind, lookup};

    fn kind(declared: &str) -> Kind {
        lookup(Some(declared), false).kind()
    }

    #[test]
    fn base_names() {
        for (declared, expected) in [
            ("INTEGER", Kind::Integer),
            ("REAL", Kind::Real),
            ("TEXT", Kind::Text),
            ("BLOB", Kind::Blob),
        ] {
            assert_eq!(kind(declared), expected, "{declared}");
        }
    }

    #[test]
    fn named_conventions_fold_case_and_strip_arguments() {
        for (declared, expected) in [
            ("BOOLEAN", Kind::Boolean),
            ("boolean", Kind::Boolean),
            ("DATETIME", Kind::DateTime),
            ("timestamp", Kind::DateTime),
            ("TIMESTAMPTZ", Kind::DateTimeTz),
            ("DateTimeTz", Kind::DateTimeTz),
            ("DATE", Kind::Date),
            ("TIME", Kind::Time),
            ("DECIMAL", Kind::Decimal),
            ("DECIMAL(10)", Kind::Decimal),
            ("decimal(10,2)", Kind::Decimal),
            ("NUMERIC (10, 2)", Kind::Decimal),
            ("  UUID  ", Kind::Uuid),
            ("JSON", Kind::Json),
            ("TIMESTAMP(6)", Kind::DateTime),
            ("TEXT(20)", Kind::Text),
        ] {
            assert_eq!(kind(declared), expected, "{declared}");
        }
    }

    #[test]
    fn affinity_substring_rules_apply_in_sqlite_order() {
        for (declared, expected) in [
            ("INT", Kind::Integer),
            ("BIGINT", Kind::Integer),
            ("UNSIGNED BIG INT", Kind::Integer),
            ("INT8", Kind::Integer),
            ("VARCHAR(255)", Kind::Text),
            ("VARYING CHARACTER(255)", Kind::Text),
            ("NCHAR(55)", Kind::Text),
            ("CLOB", Kind::Text),
            ("LONGTEXT", Kind::Text),
            ("MEDIUMBLOB", Kind::Blob),
            ("FLOAT", Kind::Real),
            ("DOUBLE", Kind::Real),
            ("DOUBLE PRECISION", Kind::Real),
            // INT wins over everything after it, CHAR over BLOB, BLOB over REAL
            ("CHARINT", Kind::Integer),
            ("CHARBLOB", Kind::Text),
            ("BLOBREAL", Kind::Blob),
        ] {
            assert_eq!(kind(declared), expected, "{declared}");
        }
    }

    #[test]
    fn numeric_affinity_decodes_by_storage_class() {
        for declared in ["ANY", "NUMBER", "TIMESTAMP WITH TIME ZONE", "STUFF"] {
            let column = lookup(Some(declared), false);
            assert_eq!(column.kind(), Kind::Any, "{declared}");
            assert!(!column.is_untyped(), "{declared}");
        }
    }

    #[test]
    fn point_and_interval_are_integers_by_the_int_substring() {
        assert_eq!(kind("POINT"), Kind::Integer);
        assert_eq!(kind("INTERVAL"), Kind::Integer);
        assert_eq!(kind("point"), Kind::Integer);
    }

    #[test]
    fn strict_tables_allow_six_types() {
        for (declared, expected) in [
            ("INT", Kind::Integer),
            ("INTEGER", Kind::Integer),
            ("integer", Kind::Integer),
            ("REAL", Kind::Real),
            ("TEXT", Kind::Text),
            ("BLOB", Kind::Blob),
            ("ANY", Kind::Any),
        ] {
            let column = lookup(Some(declared), true);
            assert_eq!(column.kind(), expected, "{declared}");
            assert!(!column.is_untyped());
        }
    }

    #[test]
    fn untyped_columns_decode_by_storage_class_and_are_nullable() {
        for declared in [None, Some(""), Some("   ")] {
            for strict in [false, true] {
                let column = lookup(declared, strict);
                assert_eq!(column.kind(), Kind::Any);
                assert!(column.is_untyped());
                assert!(column.always_nullable());
            }
        }
        assert!(!lookup(Some("INTEGER"), false).always_nullable());
    }
}

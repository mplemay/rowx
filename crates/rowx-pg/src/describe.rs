use std::collections::HashMap;

use tokio_postgres::Client;
use tokio_postgres::types::{Kind, Type};

use crate::error::Error;
use crate::types::{Matcher, TYPE_TABLE};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NullReason {
    NotNullColumn,
    NullableColumn,
    NotValidatedConstraint,
    ForeignOrView,
    OuterJoin { node: String },
    GroupingSets,
    ScanBoundary { alias: String },
    ReturningOldNew,
    Computed,
    ExplainFailed,
    OverrideBang,
    OverrideQuestion,
}

impl NullReason {
    #[must_use]
    pub const fn name(&self) -> &'static str {
        match self {
            Self::NotNullColumn => "NotNullColumn",
            Self::NullableColumn => "NullableColumn",
            Self::NotValidatedConstraint => "NotValidatedConstraint",
            Self::ForeignOrView => "ForeignOrView",
            Self::OuterJoin { .. } => "OuterJoin",
            Self::GroupingSets => "GroupingSets",
            Self::ScanBoundary { .. } => "ScanBoundary",
            Self::ReturningOldNew => "ReturningOldNew",
            Self::Computed => "Computed",
            Self::ExplainFailed => "ExplainFailed",
            Self::OverrideBang => "OverrideBang",
            Self::OverrideQuestion => "OverrideQuestion",
        }
    }
}

/// A type with no row in the type table; `typsend` is the binary send function the server reports for it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Unsupported {
    pub typsend: Option<String>,
}

pub type TypeKey = Result<&'static str, Unsupported>;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParamInfo {
    pub oid: u32,
    pub type_name: String,
    pub type_key: TypeKey,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ColumnInfo {
    pub name: String,
    pub raw_name: String,
    pub oid: u32,
    pub type_name: String,
    pub type_key: TypeKey,
    pub table_oid: Option<u32>,
    pub attnum: Option<i16>,
    pub nullable: bool,
    pub reason: NullReason,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Describe {
    pub params: Vec<ParamInfo>,
    pub columns: Vec<ColumnInfo>,
}

// a type whose send function is one of these is wire-compatible with the table row of that key
const TYPSEND_KEYS: &[(&str, &str)] = &[
    ("boolsend", "bool"),
    ("int2send", "int2"),
    ("int4send", "int4"),
    ("int8send", "int8"),
    ("float4send", "float4"),
    ("float8send", "float8"),
    ("numeric_send", "numeric"),
    ("textsend", "text"),
    ("varcharsend", "varchar"),
    ("bpcharsend", "bpchar"),
    ("byteasend", "bytea"),
    ("date_send", "date"),
    ("time_send", "time"),
    ("timetz_send", "timetz"),
    ("timestamp_send", "timestamp"),
    ("timestamptz_send", "timestamptz"),
    ("interval_send", "interval"),
    ("uuid_send", "uuid"),
    ("json_send", "json"),
    ("jsonb_send", "jsonb"),
];

fn key_of(matcher: Matcher) -> Option<&'static str> {
    TYPE_TABLE
        .iter()
        .find(|t| t.matcher == matcher)
        .map(|t| t.key)
}

fn table_key(ty: &Type) -> Option<&'static str> {
    TYPE_TABLE
        .iter()
        .find(|t| match t.matcher {
            Matcher::Oid(oid) => oid == ty.oid(),
            Matcher::NamedSimple(name) => name == ty.name(),
            _ => false,
        })
        .map(|t| t.key)
}

// the oid of the first leaf type (below any array or domain wrapper) that has no table row
fn unresolved_leaf(ty: &Type) -> Option<u32> {
    match ty.kind() {
        Kind::Enum(_) => None,
        Kind::Array(inner) | Kind::Domain(inner) => unresolved_leaf(inner),
        _ => table_key(ty).is_none().then(|| ty.oid()),
    }
}

fn resolve(ty: &Type, sends: &HashMap<u32, String>) -> TypeKey {
    let wrapper = |matcher| key_of(matcher).ok_or(Unsupported { typsend: None });
    match ty.kind() {
        Kind::Enum(_) => wrapper(Matcher::EnumKind),
        Kind::Array(inner) => resolve(inner, sends).and_then(|_| wrapper(Matcher::ArrayKind)),
        Kind::Domain(base) => resolve(base, sends).and_then(|_| wrapper(Matcher::DomainKind)),
        _ => table_key(ty).ok_or(()).or_else(|()| {
            let typsend = sends.get(&ty.oid());
            typsend
                .and_then(|send| {
                    TYPSEND_KEYS
                        .iter()
                        .find(|(name, _)| name == send)
                        .map(|(_, key)| *key)
                })
                .ok_or_else(|| Unsupported {
                    typsend: typsend.cloned(),
                })
        }),
    }
}

async fn typsends(client: &Client, oids: &[u32]) -> Result<HashMap<u32, String>, Error> {
    if oids.is_empty() {
        return Ok(HashMap::new());
    }
    let rows = client
        .query(
            "SELECT oid, typsend::text FROM pg_type WHERE oid = ANY($1::oid[])",
            &[&oids],
        )
        .await?;
    rows.iter()
        .map(|row| Ok((row.try_get(0)?, row.try_get(1)?)))
        .collect::<Result<_, tokio_postgres::Error>>()
        .map_err(Error::from)
}

/// Prepares `sql` and reports its parameter and column types. Nothing is executed.
///
/// A multi-statement string is rejected by Parse, which later steps rely on: they concatenate the
/// SQL into a simple-protocol query.
///
/// # Errors
/// `Db` (with the error position) when the server rejects the SQL, `Interface` for IO problems.
pub async fn describe(client: &Client, sql: &str) -> Result<Describe, Error> {
    let stmt = client.prepare(sql).await?;
    let mut unresolved: Vec<u32> = stmt
        .params()
        .iter()
        .chain(stmt.columns().iter().map(tokio_postgres::Column::type_))
        .filter_map(unresolved_leaf)
        .collect();
    unresolved.sort_unstable();
    unresolved.dedup();
    let sends = typsends(client, &unresolved).await?;

    let params = stmt
        .params()
        .iter()
        .map(|ty| ParamInfo {
            oid: ty.oid(),
            type_name: ty.name().to_owned(),
            type_key: resolve(ty, &sends),
        })
        .collect();
    let columns = stmt
        .columns()
        .iter()
        .map(|col| {
            let table_oid = col.table_oid().filter(|oid| *oid != 0);
            ColumnInfo {
                name: col.name().to_owned(),
                raw_name: col.name().to_owned(),
                oid: col.type_().oid(),
                type_name: col.type_().name().to_owned(),
                type_key: resolve(col.type_(), &sends),
                table_oid,
                attnum: table_oid.and_then(|_| col.column_id()),
                // base nullability lands in T32 and the plan pass in T33; until then every column is
                // conservatively nullable
                nullable: true,
                reason: if table_oid.is_some() {
                    NullReason::NullableColumn
                } else {
                    NullReason::Computed
                },
            }
        })
        .collect();
    Ok(Describe { params, columns })
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;

    use tokio_postgres::types::{Kind, Type};

    use super::{Unsupported, resolve, unresolved_leaf};

    fn custom(name: &str, oid: u32) -> Type {
        Type::new(name.to_owned(), oid, Kind::Simple, "public".to_owned())
    }

    #[test]
    fn builtin_types_resolve_without_typsend() {
        assert_eq!(resolve(&Type::INT4, &HashMap::new()), Ok("int4"));
        assert_eq!(unresolved_leaf(&Type::INT4), None);
    }

    #[test]
    fn custom_type_resolves_through_a_known_send_function() {
        let ty = custom("shell_text", 90_001);
        assert_eq!(unresolved_leaf(&ty), Some(90_001));
        let sends = HashMap::from([(90_001, "textsend".to_owned())]);
        assert_eq!(resolve(&ty, &sends), Ok("text"));
    }

    #[test]
    fn custom_type_with_other_or_no_send_function_is_unsupported() {
        let ty = custom("odd", 90_002);
        let sends = HashMap::from([(90_002, "cash_send".to_owned())]);
        assert_eq!(
            resolve(&ty, &sends),
            Err(Unsupported {
                typsend: Some("cash_send".to_owned())
            })
        );
        assert_eq!(
            resolve(&ty, &HashMap::new()),
            Err(Unsupported { typsend: None })
        );
    }

    #[test]
    fn array_of_unsupported_element_is_unsupported() {
        let array = Type::new(
            "_odd".to_owned(),
            90_003,
            Kind::Array(custom("odd", 90_002)),
            "public".to_owned(),
        );
        assert_eq!(unresolved_leaf(&array), Some(90_002));
        assert!(resolve(&array, &HashMap::new()).is_err());
    }
}

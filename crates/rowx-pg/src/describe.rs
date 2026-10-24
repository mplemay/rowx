use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};

use tokio_postgres::types::{Kind, Type};
use tokio_postgres::{Client, SimpleQueryMessage};

use crate::error::Error;
use crate::explain;
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

    /// The join node or scan alias behind the reason, when it has one.
    #[must_use]
    pub fn detail(&self) -> Option<&str> {
        match self {
            Self::OuterJoin { node } => Some(node),
            Self::ScanBoundary { alias } => Some(alias),
            _ => None,
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

// a type outside the table is unsupported even when it reuses a built-in send function, since the
// runtime codecs dispatch on the table alone; its typsend only describes why (`-` has no binary send)
fn resolve(ty: &Type, sends: &HashMap<u32, String>) -> TypeKey {
    let wrapper = |matcher| key_of(matcher).ok_or(Unsupported { typsend: None });
    match ty.kind() {
        Kind::Enum(_) => wrapper(Matcher::EnumKind),
        Kind::Array(inner) => resolve(inner, sends).and_then(|_| wrapper(Matcher::ArrayKind)),
        // the runtime encodes and decodes a domain as its base type, so it takes the base key
        Kind::Domain(base) => resolve(base, sends),
        _ => table_key(ty).ok_or_else(|| Unsupported {
            typsend: sends.get(&ty.oid()).cloned(),
        }),
    }
}

static CATALOG_QUERIES: AtomicU64 = AtomicU64::new(0);

/// How many base-nullability catalog queries this process has run; tests use it to prove batching.
#[must_use]
pub fn catalog_queries() -> u64 {
    CATALOG_QUERIES.load(Ordering::Acquire)
}

// one round trip for every column with a table origin; foreign tables and views are never NotNull
const BASE_NULLABILITY_SQL: &str = "\
SELECT o.relid, o.attnum, CASE \
    WHEN c.relkind NOT IN ('r', 'p', 'm') THEN 'foreign_or_view' \
    WHEN NOT a.attnotnull THEN 'nullable' \
    WHEN EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conrelid = a.attrelid AND k.contype = 'n' \
        AND NOT k.convalidated AND k.conkey = ARRAY[a.attnum]) THEN 'not_validated' \
    ELSE 'not_null' END \
FROM unnest($1::oid[], $2::int2[]) AS o(relid, attnum) \
JOIN pg_attribute a ON a.attrelid = o.relid AND a.attnum = o.attnum \
JOIN pg_class c ON c.oid = a.attrelid";

async fn base_nullability(
    client: &Client,
    origins: &[(u32, i16)],
) -> Result<HashMap<(u32, i16), NullReason>, Error> {
    if origins.is_empty() {
        return Ok(HashMap::new());
    }
    let (relids, attnums): (Vec<u32>, Vec<i16>) = origins.iter().copied().unzip();
    CATALOG_QUERIES.fetch_add(1, Ordering::AcqRel);
    let rows = client
        .query(BASE_NULLABILITY_SQL, &[&relids, &attnums])
        .await?;
    rows.iter()
        .map(|row| {
            let status: String = row.try_get(2)?;
            let reason = match status.as_str() {
                "not_null" => NullReason::NotNullColumn,
                "not_validated" => NullReason::NotValidatedConstraint,
                "foreign_or_view" => NullReason::ForeignOrView,
                _ => NullReason::NullableColumn,
            };
            Ok(((row.try_get(0)?, row.try_get(1)?), reason))
        })
        .collect::<Result<_, tokio_postgres::Error>>()
        .map_err(Error::from)
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

// simple_query, because the extended protocol would demand values for `$n`; the caller's Parse has
// already rejected anything but a single statement
async fn plan_promotions(
    client: &Client,
    sql: &str,
    column_count: usize,
) -> Option<Vec<Option<NullReason>>> {
    let messages = client
        .simple_query(&format!(
            "EXPLAIN (GENERIC_PLAN, VERBOSE, COSTS OFF, FORMAT JSON) {sql}"
        ))
        .await
        .ok()?;
    let text: String = messages
        .iter()
        .filter_map(|message| match message {
            SimpleQueryMessage::Row(row) => row.get(0),
            _ => None,
        })
        .collect();
    let plan = serde_json::from_str(&text).ok()?;
    Some(explain::walk(&plan, column_count))
}

fn apply_overrides(col: &mut ColumnInfo) {
    // the server names an unnamed expression `?column?`, which is not an override
    if col.raw_name == "?column?" {
        return;
    }
    let (stripped, reason, nullable) = if let Some(rest) = col.raw_name.strip_suffix('!') {
        (rest, NullReason::OverrideBang, false)
    } else if let Some(rest) = col.raw_name.strip_suffix('?') {
        (rest, NullReason::OverrideQuestion, true)
    } else {
        return;
    };
    col.name = stripped.to_owned();
    col.nullable = nullable;
    col.reason = reason;
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
    let mut columns: Vec<ColumnInfo> = stmt
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
                // the catalog and plan passes below refine this
                nullable: true,
                reason: if table_oid.is_some() {
                    NullReason::NullableColumn
                } else {
                    NullReason::Computed
                },
            }
        })
        .collect();

    let mut origins: Vec<(u32, i16)> = columns
        .iter()
        .filter_map(|c| c.table_oid.zip(c.attnum))
        .collect();
    origins.sort_unstable();
    origins.dedup();
    let base = base_nullability(client, &origins).await?;
    for col in &mut columns {
        if let Some(reason) = col.table_oid.zip(col.attnum).and_then(|o| base.get(&o)) {
            col.nullable = *reason != NullReason::NotNullColumn;
            col.reason = reason.clone();
        }
    }

    if !columns.is_empty() {
        match plan_promotions(client, sql, columns.len()).await {
            Some(promotions) => {
                for (col, promotion) in columns.iter_mut().zip(promotions) {
                    if let Some(reason) = promotion {
                        col.nullable = true;
                        col.reason = reason;
                    }
                }
            }
            None => {
                for col in &mut columns {
                    if col.reason == NullReason::Computed {
                        col.reason = NullReason::ExplainFailed;
                    }
                }
            }
        }
    }
    // Unknown columns (Computed, ExplainFailed) were never set to NotNull, so they are already Nullable
    columns.iter_mut().for_each(apply_overrides);
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
    fn custom_type_with_a_builtin_send_function_is_unsupported() {
        let ty = custom("refcursor_like", 90_001);
        assert_eq!(unresolved_leaf(&ty), Some(90_001));
        let sends = HashMap::from([(90_001, "textsend".to_owned())]);
        assert_eq!(
            resolve(&ty, &sends),
            Err(Unsupported {
                typsend: Some("textsend".to_owned())
            })
        );
    }

    #[test]
    fn domain_resolves_to_its_base_key() {
        let domain = Type::new(
            "posint".to_owned(),
            90_004,
            Kind::Domain(Type::INT4),
            "public".to_owned(),
        );
        assert_eq!(resolve(&domain, &HashMap::new()), Ok("int4"));
        let over_unsupported = Type::new(
            "odd_domain".to_owned(),
            90_005,
            Kind::Domain(custom("odd", 90_002)),
            "public".to_owned(),
        );
        assert!(resolve(&over_unsupported, &HashMap::new()).is_err());
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

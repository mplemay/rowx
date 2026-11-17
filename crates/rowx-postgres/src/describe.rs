use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};

use rowx_core::row_shape::{Override, column_name};
use serde_json::Value;
use tokio_postgres::types::{Kind, Type};
use tokio_postgres::{Client, SimpleQueryMessage, Statement};

use crate::error::Error;
use crate::explain::{self, Origin};
use crate::types::{Matcher, TYPE_TABLE};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NullReason {
    NotNullColumn,
    NullableColumn,
    ScanFilterNotNull,
    NotValidatedConstraint,
    ForeignOrView,
    ViewColumn,
    OuterJoin { node: String },
    GroupingSets,
    ScanBoundary { alias: String },
    ReturningOldNew,
    SetOperation,
    ComputedExpr,
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
            Self::ScanFilterNotNull => "ScanFilterNotNull",
            Self::NotValidatedConstraint => "NotValidatedConstraint",
            Self::ForeignOrView => "ForeignOrView",
            Self::ViewColumn => "ViewColumn",
            Self::OuterJoin { .. } => "OuterJoin",
            Self::GroupingSets => "GroupingSets",
            Self::ScanBoundary { .. } => "ScanBoundary",
            Self::ReturningOldNew => "ReturningOldNew",
            Self::SetOperation => "SetOperation",
            Self::ComputedExpr => "ComputedExpr",
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
    // the table key of an array parameter's element, `None` for any other parameter or an unsupported element
    pub element_key: Option<&'static str>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ColumnInfo {
    pub name: String,
    pub raw_name: String,
    pub oid: u32,
    pub type_name: String,
    pub type_key: TypeKey,
    // the table key of an array column's element, `None` for any other column or an unsupported element
    pub element_key: Option<&'static str>,
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

fn range_key(subtype: &Type) -> Option<&'static str> {
    key_of(Matcher::RangeKind(subtype.oid()))
}

// the extension that owns each candidate type, by oid; a type missing from it belongs to none
pub type Owners = HashMap<u32, String>;

fn table_key(ty: &Type, owners: &Owners) -> Option<&'static str> {
    TYPE_TABLE
        .iter()
        .find(|t| match t.matcher {
            Matcher::Oid(oid) => oid == ty.oid(),
            Matcher::ExtNamed(name, extension) => {
                matches!(ty.kind(), Kind::Simple)
                    && name == ty.name()
                    && owners.get(&ty.oid()).is_some_and(|o| o == extension)
            }
            _ => false,
        })
        .map(|t| t.key)
}

// the oid of the leaf type (below any array or domain wrapper) that bears an extension row's name,
// which is the only kind of type whose owner decides its row
fn extension_leaf(ty: &Type) -> Option<&Type> {
    match ty.kind() {
        Kind::Array(inner) | Kind::Domain(inner) => extension_leaf(inner),
        Kind::Simple => TYPE_TABLE
            .iter()
            .any(|t| matches!(t.matcher, Matcher::ExtNamed(name, _) if name == ty.name()))
            .then_some(ty),
        _ => None,
    }
}

/// The oids among `types` whose owning extension decides how they are read (sorted, deduplicated).
#[must_use]
pub fn extension_candidates<'a>(types: impl IntoIterator<Item = &'a Type>) -> Vec<u32> {
    let mut oids: Vec<u32> = types
        .into_iter()
        .filter_map(|ty| extension_leaf(ty).map(Type::oid))
        .collect();
    oids.sort_unstable();
    oids.dedup();
    oids
}

/// The name of the first type among `types` that bears an extension type's name without being owned
/// by that extension; the runtime codecs dispatch on the name, so a statement using it must not run.
#[must_use]
pub fn impostor<'a>(types: impl IntoIterator<Item = &'a Type>, owners: &Owners) -> Option<String> {
    types
        .into_iter()
        .filter_map(extension_leaf)
        .find(|leaf| table_key(leaf, owners).is_none())
        .map(|leaf| format!("{}.{}", leaf.schema(), leaf.name()))
}

/// The extension (`pg_depend` with `deptype = 'e'`, then `pg_extension`) that owns each type in `oids`.
/// A type that no extension owns is absent from the result.
///
/// # Errors
/// `Db`/`Interface` when the catalog query fails.
pub async fn extension_owners(client: &Client, oids: &[u32]) -> Result<Owners, Error> {
    if oids.is_empty() {
        return Ok(Owners::new());
    }
    let rows = client
        .query(
            "SELECT d.objid, e.extname::pg_catalog.text FROM pg_catalog.pg_depend d \
             JOIN pg_catalog.pg_extension e ON e.oid OPERATOR(pg_catalog.=) d.refobjid \
             WHERE d.classid OPERATOR(pg_catalog.=) 'pg_catalog.pg_type'::pg_catalog.regclass \
             AND d.refclassid OPERATOR(pg_catalog.=) 'pg_catalog.pg_extension'::pg_catalog.regclass \
             AND d.deptype OPERATOR(pg_catalog.=) 'e' AND d.objid OPERATOR(pg_catalog.=) ANY($1::pg_catalog.oid[])",
            &[&oids],
        )
        .await?;
    rows.iter()
        .map(|row| Ok((row.try_get(0)?, row.try_get(1)?)))
        .collect::<Result<_, tokio_postgres::Error>>()
        .map_err(Error::from)
}

// the oid of the first leaf type (below any array or domain wrapper) that has no table row
fn unresolved_leaf(ty: &Type, owners: &Owners) -> Option<u32> {
    match ty.kind() {
        Kind::Enum(_) => None,
        Kind::Array(inner) | Kind::Domain(inner) => unresolved_leaf(inner, owners),
        Kind::Range(subtype) => range_key(subtype).is_none().then(|| ty.oid()),
        _ => table_key(ty, owners).is_none().then(|| ty.oid()),
    }
}

// a type outside the table is unsupported even when it reuses a built-in send function, since the
// runtime codecs dispatch on the table alone; its typsend only describes why (`-` has no binary send)
fn resolve(ty: &Type, sends: &HashMap<u32, String>, owners: &Owners) -> TypeKey {
    let wrapper = |matcher| key_of(matcher).ok_or(Unsupported { typsend: None });
    match ty.kind() {
        Kind::Enum(_) => wrapper(Matcher::EnumKind),
        Kind::Array(inner) => {
            resolve(inner, sends, owners).and_then(|_| wrapper(Matcher::ArrayKind))
        }
        // the runtime encodes and decodes a domain as its base type, so it takes the base key
        Kind::Domain(base) => resolve(base, sends, owners),
        // a user-defined range over a supported subtype decodes like the built-in range of that subtype
        Kind::Range(subtype) => range_key(subtype).ok_or_else(|| Unsupported {
            typsend: sends.get(&ty.oid()).cloned(),
        }),
        _ => table_key(ty, owners).ok_or_else(|| Unsupported {
            typsend: sends.get(&ty.oid()).cloned(),
        }),
    }
}

// the key of the element type behind an array (looking through domains), if there is one
fn element_key(ty: &Type, sends: &HashMap<u32, String>, owners: &Owners) -> Option<&'static str> {
    match ty.kind() {
        Kind::Array(inner) => resolve(inner, sends, owners).ok(),
        Kind::Domain(base) => element_key(base, sends, owners),
        _ => None,
    }
}

static CATALOG_QUERIES: AtomicU64 = AtomicU64::new(0);

/// How many base-nullability catalog queries this process has run; tests use it to prove batching.
#[must_use]
pub fn catalog_queries() -> u64 {
    CATALOG_QUERIES.load(Ordering::Acquire)
}

// one round trip for every column with a table origin; foreign tables and views are never NotNull here, and a
// view is resolved afterwards from its definition
const BASE_NULLABILITY_SQL: &str = "\
SELECT o.relid, o.attnum, n.nspname::pg_catalog.text, c.relname::pg_catalog.text, a.attname::pg_catalog.text, CASE \
    WHEN c.relkind OPERATOR(pg_catalog.=) 'v' THEN 'view' \
    WHEN c.relkind OPERATOR(pg_catalog.<>) ALL (ARRAY['r', 'p', 'm']::pg_catalog.\"char\"[]) THEN 'foreign_or_view' \
    WHEN NOT a.attnotnull THEN 'nullable' \
    WHEN EXISTS (SELECT 1 FROM pg_catalog.pg_constraint k WHERE k.conrelid OPERATOR(pg_catalog.=) a.attrelid AND k.contype OPERATOR(pg_catalog.=) 'n' \
        AND NOT k.convalidated AND k.conkey OPERATOR(pg_catalog.=) ARRAY[a.attnum]) THEN 'not_validated' \
    ELSE 'not_null' END \
FROM ROWS FROM (pg_catalog.unnest($1::pg_catalog.oid[]), pg_catalog.unnest($2::pg_catalog.int2[])) AS o(relid, attnum) \
JOIN pg_catalog.pg_attribute a ON a.attrelid OPERATOR(pg_catalog.=) o.relid AND a.attnum OPERATOR(pg_catalog.=) o.attnum \
JOIN pg_catalog.pg_class c ON c.oid OPERATOR(pg_catalog.=) a.attrelid \
JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) c.relnamespace";

// the verdict, the origin and whether the relation is a view
type Base = HashMap<(u32, i16), (NullReason, Origin, bool)>;

async fn base_nullability(client: &Client, origins: &[(u32, i16)]) -> Result<Base, Error> {
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
            let status: String = row.try_get(5)?;
            let reason = match status.as_str() {
                "not_null" => NullReason::NotNullColumn,
                "not_validated" => NullReason::NotValidatedConstraint,
                "foreign_or_view" | "view" => NullReason::ForeignOrView,
                _ => NullReason::NullableColumn,
            };
            let origin = Origin {
                schema: row.try_get(2)?,
                relation: row.try_get(3)?,
                column: row.try_get(4)?,
            };
            Ok((
                (row.try_get(0)?, row.try_get(1)?),
                (reason, origin, status == "view"),
            ))
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
            "SELECT oid, typsend::pg_catalog.text FROM pg_catalog.pg_type WHERE oid OPERATOR(pg_catalog.=) ANY($1::pg_catalog.oid[])",
            &[&oids],
        )
        .await?;
    rows.iter()
        .map(|row| Ok((row.try_get(0)?, row.try_get(1)?)))
        .collect::<Result<_, tokio_postgres::Error>>()
        .map_err(Error::from)
}

// the leaf columns that are NOT NULL on a real table (never a view, foreign table or NOT VALID constraint);
// a failed lookup proves nothing, so it is an empty set
const LEAF_NOT_NULL_SQL: &str = "\
SELECT l.s, l.r, l.c FROM ROWS FROM (pg_catalog.unnest($1::pg_catalog.text[]), pg_catalog.unnest($2::pg_catalog.text[]), \
    pg_catalog.unnest($3::pg_catalog.text[])) AS l(s, r, c) \
JOIN pg_catalog.pg_namespace n ON n.nspname OPERATOR(pg_catalog.=) l.s \
JOIN pg_catalog.pg_class c ON c.relnamespace OPERATOR(pg_catalog.=) n.oid AND c.relname OPERATOR(pg_catalog.=) l.r \
    AND c.relkind OPERATOR(pg_catalog.=) ANY (ARRAY['r', 'p']::pg_catalog.\"char\"[]) \
JOIN pg_catalog.pg_attribute a ON a.attrelid OPERATOR(pg_catalog.=) c.oid AND a.attname OPERATOR(pg_catalog.=) l.c AND a.attnotnull AND NOT a.attisdropped \
WHERE NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint k WHERE k.conrelid OPERATOR(pg_catalog.=) a.attrelid AND k.contype OPERATOR(pg_catalog.=) 'n' \
    AND NOT k.convalidated AND k.conkey OPERATOR(pg_catalog.=) ARRAY[a.attnum])";

async fn not_null_leaves(client: &Client, leaves: &[Origin]) -> explain::NotNullLeaves {
    if leaves.is_empty() {
        return explain::NotNullLeaves::new();
    }
    let schemas: Vec<&str> = leaves.iter().map(|o| o.schema.as_str()).collect();
    let relations: Vec<&str> = leaves.iter().map(|o| o.relation.as_str()).collect();
    let columns: Vec<&str> = leaves.iter().map(|o| o.column.as_str()).collect();
    let Ok(rows) = client
        .query(LEAF_NOT_NULL_SQL, &[&schemas, &relations, &columns])
        .await
    else {
        return explain::NotNullLeaves::new();
    };
    rows.iter()
        .filter_map(|row| {
            Some(Origin {
                schema: row.try_get(0).ok()?,
                relation: row.try_get(1).ok()?,
                column: row.try_get(2).ok()?,
            })
        })
        .collect()
}

// A function with a whitelisted name outside `pg_catalog` may shadow the built-in under the session's
// `search_path`, because the plan prints functions by bare name. A failed lookup is treated as a shadow.
const SHADOWED_FUNCTIONS_SQL: &str = "\
SELECT 1 FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) p.pronamespace \
WHERE p.proname OPERATOR(pg_catalog.=) ANY($1::pg_catalog.text[]) AND n.nspname OPERATOR(pg_catalog.<>) 'pg_catalog' LIMIT 1";

async fn functions_are_unshadowed(client: &Client) -> bool {
    let names: Vec<&str> = explain::WHITELISTED_FUNCTIONS.to_vec();
    client
        .query(SHADOWED_FUNCTIONS_SQL, &[&names])
        .await
        .is_ok_and(|rows| rows.is_empty())
}

// Per-column nullability promotions and independent NOT NULL proofs from the plan.
struct PlanVerdicts {
    promotions: Vec<Option<NullReason>>,
    set_operation: Vec<bool>,
    computed: Vec<bool>,
    scan_filter: Vec<bool>,
}

// The EXPLAIN is a simple query, because the extended protocol would demand values for `$n`. It names the
// statement the server already parsed and never carries SQL text, so what runs is exactly what Parse accepted.
// `plan_cache_mode` is pinned for this transaction alone, whatever the session says: a custom plan would fold
// `= NULL` and drop an outer join. Each NULL is coerced to its parameter type, so no parameter may be a domain.
#[must_use]
fn explain_execute_sql(name: &str, params: usize) -> String {
    let name = format!("\"{}\"", name.replace('"', "\"\""));
    let args = if params == 0 {
        String::new()
    } else {
        format!("({})", vec!["NULL"; params].join(", "))
    };
    format!(
        "SET LOCAL plan_cache_mode = force_generic_plan; \
         EXPLAIN (VERBOSE, COSTS OFF, FORMAT JSON) EXECUTE {name}{args}"
    )
}

#[cfg(test)]
thread_local! {
    static EXPLAIN_SENT: std::cell::RefCell<Vec<String>> = const { std::cell::RefCell::new(Vec::new()) };
}

// the one place the plan pass sends text, and exactly one result set may come back
async fn explain_plan(client: &Client, text: &str) -> Option<Value> {
    #[cfg(test)]
    EXPLAIN_SENT.with(|sent| sent.borrow_mut().push(text.to_owned()));
    let messages = client.simple_query(text).await.ok()?;
    let sets = messages
        .iter()
        .filter(|message| matches!(message, SimpleQueryMessage::RowDescription(_)))
        .count();
    if sets != 1 {
        return None;
    }
    let json: String = messages
        .iter()
        .filter_map(|message| match message {
            SimpleQueryMessage::Row(row) => row.get(0),
            _ => None,
        })
        .collect();
    let plan: Value = serde_json::from_str(&json).ok()?;
    // a utility statement (`SHOW`, `CALL`) explains as the text `Utility Statement`, which has no plan
    plan.get(0)?.get("Plan")?;
    Some(plan)
}

async fn plan_promotions(
    client: &Client,
    name: &str,
    params: usize,
    column_count: usize,
    origins: &[Option<Origin>],
) -> Option<PlanVerdicts> {
    let plan = explain_plan(client, &explain_execute_sql(name, params)).await?;
    Some(analyse_plan(client, &plan, column_count, origins).await)
}

async fn analyse_plan(
    client: &Client,
    plan: &Value,
    column_count: usize,
    origins: &[Option<Origin>],
) -> PlanVerdicts {
    let leaves = not_null_leaves(client, &explain::leaf_columns(plan)).await;
    // the lookup only runs when a function name is what proves a column
    let functions = !explain::needs_function_check(plan, column_count)
        || functions_are_unshadowed(client).await;
    PlanVerdicts {
        promotions: explain::walk(plan, column_count, origins),
        set_operation: explain::proven_not_null(plan, column_count, &leaves),
        computed: explain::proven_computed(plan, column_count, functions),
        scan_filter: explain::scan_filter_not_null(plan, column_count, origins),
    }
}

// The server-side name of the statement just prepared from `sql`. A dropped statement's `Close` is
// asynchronous, so several names can match and any of them is the same statement; the parameter types keep a
// same-text statement prepared with other types from being picked. `Statement::name` is not public.
const STATEMENT_NAME_SQL: &str = "\
SELECT s.name::pg_catalog.text FROM pg_catalog.pg_prepared_statements s \
WHERE NOT s.from_sql AND s.statement OPERATOR(pg_catalog.=) $1::pg_catalog.text \
AND s.parameter_types::pg_catalog.oid[] OPERATOR(pg_catalog.=) $2::pg_catalog.oid[] \
ORDER BY s.name LIMIT 1";

async fn statement_name(client: &Client, sql: &str, params: &[Type]) -> Option<String> {
    let oids: Vec<u32> = params.iter().map(Type::oid).collect();
    client
        .query_opt(STATEMENT_NAME_SQL, &[&sql, &oids])
        .await
        .ok()??
        .try_get(0)
        .ok()
}

// `stmt` must stay alive until the plan pass is done, or the server closes it, and must have no domain parameter
async fn explain_statement(
    client: &Client,
    sql: &str,
    stmt: &Statement,
    column_count: usize,
    origins: &[Option<Origin>],
) -> Option<PlanVerdicts> {
    let name = statement_name(client, sql, stmt.params()).await?;
    plan_promotions(client, &name, stmt.params().len(), column_count, origins).await
}

// the type with every domain layer removed
fn base_type(ty: &Type) -> Type {
    match ty.kind() {
        Kind::Domain(base) => base_type(base),
        _ => ty.clone(),
    }
}

// the parameter types with each domain replaced by its base type, when there is a domain among them
fn base_param_types(params: &[Type]) -> Option<Vec<Type>> {
    params
        .iter()
        .any(|ty| matches!(ty.kind(), Kind::Domain(_)))
        .then(|| params.iter().map(base_type).collect())
}

// `EXPLAIN EXECUTE` coerces each NULL to its parameter type, which runs a domain's constraints, and a CHECK may
// call a volatile function. So a statement with a domain parameter is never explained as prepared: the same SQL
// is prepared again with each domain typed as its base type, and only that statement is explained. A NULL array,
// range or composite checks none of its elements, so only a top-level domain needs this.
async fn plan_verdicts(
    client: &Client,
    sql: &str,
    stmt: &Statement,
    column_count: usize,
    origins: &[Option<Origin>],
) -> Option<PlanVerdicts> {
    let Some(types) = base_param_types(stmt.params()) else {
        return explain_statement(client, sql, stmt, column_count, origins).await;
    };
    let based = client.prepare_typed(sql, &types).await.ok()?;
    let mut verdicts = explain_statement(client, sql, &based, column_count, origins).await?;
    // A surrogate parameter type can select a different overload and therefore a different scan filter.
    verdicts.scan_filter.fill(false);
    Some(verdicts)
}

// the nullability-relevant part of a column, so a view's definition can be analysed without its types
struct Slot {
    table_oid: Option<u32>,
    attnum: Option<i16>,
    nullable: bool,
    reason: NullReason,
}

// a view column is resolved at most 8 views deep
const MAX_VIEW_DEPTH: usize = 8;

#[derive(Default)]
struct ViewCtx {
    // inside the rolled-back transaction that pins `search_path`
    pinned: bool,
    // the chain of views being resolved, which catches a cycle and bounds the depth
    visited: Vec<u32>,
    // per view: whether each output column is NOT NULL, or `None` when the view falls back to Nullable
    cache: HashMap<u32, Option<Vec<bool>>>,
}

// One catalog query (base nullability), the views behind the view columns, and the plan pass. Returns whether
// the plan pass ran; when it did not, a view definition is not trusted. `stmt` is `sql` prepared.
async fn refine(
    client: &Client,
    sql: &str,
    stmt: &Statement,
    slots: &mut [Slot],
    ctx: &mut ViewCtx,
) -> Result<bool, Error> {
    let mut origins: Vec<(u32, i16)> = slots
        .iter()
        .filter_map(|c| c.table_oid.zip(c.attnum))
        .collect();
    origins.sort_unstable();
    origins.dedup();
    let base = base_nullability(client, &origins).await?;
    let mut column_origins = Vec::with_capacity(slots.len());
    for slot in slots.iter_mut() {
        let found = slot.table_oid.zip(slot.attnum).and_then(|o| base.get(&o));
        if let Some((reason, _, is_view)) = found {
            slot.nullable = *reason != NullReason::NotNullColumn;
            slot.reason = reason.clone();
            if *is_view
                && let Some((oid, attnum)) = slot.table_oid.zip(slot.attnum)
                && view_column_not_null(client, oid, attnum, ctx).await
            {
                slot.nullable = false;
                slot.reason = NullReason::ViewColumn;
            }
        }
        column_origins.push(found.map(|(_, origin, _)| origin.clone()));
    }

    if slots.is_empty() {
        return Ok(true);
    }
    if let Some(verdicts) = plan_verdicts(client, sql, stmt, slots.len(), &column_origins).await {
        for (((((slot, promotion), proven), computed), scan_filter), column) in slots
            .iter_mut()
            .zip(verdicts.promotions)
            .zip(verdicts.set_operation)
            .zip(verdicts.computed)
            .zip(verdicts.scan_filter)
            .zip(stmt.columns())
        {
            if slot.reason == NullReason::Computed && (proven || computed) {
                slot.nullable = false;
                slot.reason = if computed {
                    NullReason::ComputedExpr
                } else {
                    NullReason::SetOperation
                };
            }
            if slot.reason == NullReason::NullableColumn
                && scan_filter
                && matches!(
                    base_type(column.type_()).kind(),
                    Kind::Simple | Kind::Enum(_)
                )
            {
                slot.nullable = false;
                slot.reason = NullReason::ScanFilterNotNull;
            }
            if let Some(reason) = promotion {
                slot.nullable = true;
                slot.reason = reason;
            }
        }
        Ok(true)
    } else {
        for slot in slots.iter_mut() {
            if slot.reason == NullReason::Computed {
                slot.reason = NullReason::ExplainFailed;
            }
        }
        Ok(false)
    }
}

// the position of `attnum` among the live columns, which are the view definition's output columns in order
fn output_position(live_attnums: &[i16], attnum: i16) -> Option<usize> {
    live_attnums.iter().position(|n| *n == attnum)
}

// The definition without its trailing `;`, which Parse accepts but the statement text does not need
fn definition_statement(definition: &str) -> Option<&str> {
    let text = definition.trim_end();
    let text = text.strip_suffix(';').unwrap_or(text);
    (!text.trim().is_empty()).then_some(text)
}

async fn view_column_not_null(client: &Client, view: u32, attnum: i16, ctx: &mut ViewCtx) -> bool {
    let Some(verdicts) = view_verdicts(client, view, ctx).await else {
        return false;
    };
    let Some(live) = live_attnums(client, view).await else {
        return false;
    };
    // a view without a matching shape is not trusted
    live.len() == verdicts.len()
        && output_position(&live, attnum).is_some_and(|i| verdicts.get(i).copied().unwrap_or(false))
}

async fn live_attnums(client: &Client, view: u32) -> Option<Vec<i16>> {
    let rows = client
        .query(
            "SELECT attnum FROM pg_catalog.pg_attribute \
             WHERE attrelid OPERATOR(pg_catalog.=) $1::pg_catalog.oid AND attnum OPERATOR(pg_catalog.>) 0 AND NOT attisdropped \
             ORDER BY attnum",
            &[&view],
        )
        .await
        .ok()?;
    rows.iter().map(|r| r.try_get(0).ok()).collect()
}

async fn view_verdicts(client: &Client, view: u32, ctx: &mut ViewCtx) -> Option<Vec<bool>> {
    if let Some(cached) = ctx.cache.get(&view) {
        return cached.clone();
    }
    if ctx.visited.contains(&view) || ctx.visited.len() >= MAX_VIEW_DEPTH {
        return None;
    }
    ctx.visited.push(view);
    let result = pinned_view_verdicts(client, view, ctx).await;
    ctx.visited.pop();
    ctx.cache.insert(view, result.clone());
    result
}

// `SET LOCAL search_path = pg_catalog` makes `pg_get_viewdef` qualify every name, so the definition cannot
// resolve to a different table under the checker's own path; the transaction is always rolled back
async fn pinned_view_verdicts(client: &Client, view: u32, ctx: &mut ViewCtx) -> Option<Vec<bool>> {
    let outermost = !ctx.pinned;
    if outermost {
        if client
            .batch_execute("BEGIN; SET LOCAL search_path = pg_catalog")
            .await
            .is_err()
        {
            let _ = client.batch_execute("ROLLBACK").await;
            return None;
        }
        ctx.pinned = true;
    }
    let result = savepointed_view_verdicts(client, view, ctx).await;
    if outermost {
        ctx.pinned = false;
        let _ = client.batch_execute("ROLLBACK").await;
    }
    result
}

// a savepoint keeps a failed definition from aborting the transaction for the views that follow
async fn savepointed_view_verdicts(
    client: &Client,
    view: u32,
    ctx: &mut ViewCtx,
) -> Option<Vec<bool>> {
    client.batch_execute("SAVEPOINT rowx_view").await.ok()?;
    let verdicts = definition_verdicts(client, view, ctx).await;
    let end = if verdicts.is_some() {
        "RELEASE SAVEPOINT rowx_view"
    } else {
        "ROLLBACK TO SAVEPOINT rowx_view"
    };
    client.batch_execute(end).await.ok()?;
    verdicts
}

// the view is eligible unless it has a rule besides `_RETURN`, or is `security_invoker` over a relation the
// role cannot read
const VIEW_ELIGIBLE_SQL: &str = "\
SELECT c.relkind OPERATOR(pg_catalog.=) 'v' \
    AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_rewrite r WHERE r.ev_class OPERATOR(pg_catalog.=) c.oid \
        AND r.rulename OPERATOR(pg_catalog.<>) '_RETURN') \
    AND (NOT COALESCE((SELECT pg_catalog.bool_or(o.option_value::boolean) \
            FROM pg_catalog.pg_options_to_table(c.reloptions) o \
            WHERE o.option_name OPERATOR(pg_catalog.=) 'security_invoker'), false) \
        OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_rewrite r \
            JOIN pg_catalog.pg_depend d ON d.classid OPERATOR(pg_catalog.=) 'pg_catalog.pg_rewrite'::pg_catalog.regclass \
                AND d.objid OPERATOR(pg_catalog.=) r.oid \
                AND d.refclassid OPERATOR(pg_catalog.=) 'pg_catalog.pg_class'::pg_catalog.regclass \
                AND d.refobjid OPERATOR(pg_catalog.<>) c.oid \
            WHERE r.ev_class OPERATOR(pg_catalog.=) c.oid AND NOT pg_catalog.has_table_privilege(d.refobjid, 'SELECT'))) \
FROM pg_catalog.pg_class c WHERE c.oid OPERATOR(pg_catalog.=) $1::pg_catalog.oid";

async fn definition_verdicts(client: &Client, view: u32, ctx: &mut ViewCtx) -> Option<Vec<bool>> {
    let eligible: bool = client
        .query_opt(VIEW_ELIGIBLE_SQL, &[&view])
        .await
        .ok()??
        .try_get(0)
        .ok()?;
    if !eligible {
        return None;
    }
    let definition: String = client
        .query_one(
            "SELECT pg_catalog.pg_get_viewdef($1::pg_catalog.oid, false)",
            &[&view],
        )
        .await
        .ok()?
        .try_get(0)
        .ok()?;
    analyse_definition(client, &definition, ctx).await
}

// Parse rejects anything but a single statement, as for the user's own query; a definition that fails Parse, or
// carries a second statement, is Nullable. The plan pass names the prepared statement and never sends the text.
async fn analyse_definition(
    client: &Client,
    definition: &str,
    ctx: &mut ViewCtx,
) -> Option<Vec<bool>> {
    let sql = definition_statement(definition)?;
    let stmt = client.prepare(sql).await.ok()?;
    let mut slots: Vec<Slot> = stmt
        .columns()
        .iter()
        .map(|col| {
            let table_oid = col.table_oid().filter(|oid| *oid != 0);
            Slot {
                table_oid,
                attnum: table_oid.and_then(|_| col.column_id()),
                nullable: true,
                reason: if table_oid.is_some() {
                    NullReason::NullableColumn
                } else {
                    NullReason::Computed
                },
            }
        })
        .collect();
    let plan_ran = Box::pin(refine(client, sql, &stmt, &mut slots, ctx))
        .await
        .ok()?;
    plan_ran.then(|| slots.iter().map(|s| !s.nullable).collect())
}

fn apply_overrides(col: &mut ColumnInfo) {
    let (stripped, nullable, reason) = match column_name(&col.raw_name) {
        (name, Some(Override::Bang)) => (name, false, NullReason::OverrideBang),
        (name, Some(Override::Question)) => (name, true, NullReason::OverrideQuestion),
        (_, None) => return,
    };
    col.name = stripped.to_owned();
    col.nullable = nullable;
    col.reason = reason;
}

/// Prepares `sql` and reports its parameter and column types. Nothing is executed.
///
/// A multi-statement string is rejected by Parse. The plan pass names the prepared statement and never
/// sends the SQL text again.
///
/// # Errors
/// `Db` (with the error position) when the server rejects the SQL, `Interface` for IO problems.
pub async fn describe(client: &Client, sql: &str) -> Result<Describe, Error> {
    let stmt = client.prepare(sql).await?;
    let owners = extension_owners(
        client,
        &extension_candidates(
            stmt.params()
                .iter()
                .chain(stmt.columns().iter().map(tokio_postgres::Column::type_)),
        ),
    )
    .await?;
    let mut unresolved: Vec<u32> = stmt
        .params()
        .iter()
        .chain(stmt.columns().iter().map(tokio_postgres::Column::type_))
        .filter_map(|ty| unresolved_leaf(ty, &owners))
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
            type_key: resolve(ty, &sends, &owners),
            element_key: element_key(ty, &sends, &owners),
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
                type_key: resolve(col.type_(), &sends, &owners),
                element_key: element_key(col.type_(), &sends, &owners),
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

    let mut slots: Vec<Slot> = columns
        .iter()
        .map(|c| Slot {
            table_oid: c.table_oid,
            attnum: c.attnum,
            nullable: c.nullable,
            reason: c.reason.clone(),
        })
        .collect();
    refine(client, sql, &stmt, &mut slots, &mut ViewCtx::default()).await?;
    for (col, slot) in columns.iter_mut().zip(slots) {
        col.nullable = slot.nullable;
        col.reason = slot.reason;
    }
    // Unknown columns (Computed, ExplainFailed) were never set to NotNull, so they are already Nullable
    columns.iter_mut().for_each(apply_overrides);
    Ok(Describe { params, columns })
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;

    use tokio_postgres::types::{Kind, Type};

    use super::{
        EXPLAIN_SENT, Unsupported, ViewCtx, analyse_definition, base_param_types,
        definition_statement, describe, explain_execute_sql, extension_candidates, impostor,
        output_position, resolve, unresolved_leaf,
    };

    const MARKER: &str = "rowx_user_sql_marker_5d2c";

    #[test]
    fn explain_execute_has_no_parentheses_without_parameters() {
        assert_eq!(
            explain_execute_sql("s0", 0),
            "SET LOCAL plan_cache_mode = force_generic_plan; \
             EXPLAIN (VERBOSE, COSTS OFF, FORMAT JSON) EXECUTE \"s0\""
        );
    }

    #[test]
    fn explain_execute_binds_one_null_per_parameter() {
        let tail = |sql: String| sql.rsplit(" EXECUTE ").next().map(str::to_owned);
        assert_eq!(
            tail(explain_execute_sql("s1", 1)).as_deref(),
            Some("\"s1\"(NULL)")
        );
        assert_eq!(
            tail(explain_execute_sql("s1", 3)).as_deref(),
            Some("\"s1\"(NULL, NULL, NULL)")
        );
        let many = explain_execute_sql("s1", 1000);
        assert_eq!(many.matches("NULL").count(), 1000);
        assert!(many.ends_with("NULL)"));
    }

    #[test]
    fn explain_execute_quotes_the_name_as_an_identifier() {
        assert!(
            explain_execute_sql("a\"b", 0).ends_with("EXECUTE \"a\"\"b\""),
            "a quote is doubled"
        );
        // the doubled quote keeps the whole name inside one identifier, so it cannot end the statement
        let sql = explain_execute_sql("x\"; DROP TABLE t; --", 2);
        assert!(sql.ends_with("EXECUTE \"x\"\"; DROP TABLE t; --\"(NULL, NULL)"));
    }

    #[test]
    fn explain_execute_never_contains_the_users_sql() {
        // the builder receives a name and a count, so nothing of the statement can reach it
        for params in [0, 1, 7, 100] {
            let sql = explain_execute_sql("s42", params);
            assert!(!sql.contains(MARKER), "{sql}");
        }
    }

    #[test]
    fn domains_are_replaced_by_their_base_type_at_any_depth() {
        let inner = Type::new(
            "inner_dom".to_owned(),
            90_040,
            Kind::Domain(Type::INT4),
            "public".to_owned(),
        );
        let outer = Type::new(
            "outer_dom".to_owned(),
            90_041,
            Kind::Domain(inner),
            "public".to_owned(),
        );
        assert_eq!(
            base_param_types(&[outer, Type::TEXT]),
            Some(vec![Type::INT4, Type::TEXT])
        );
        assert_eq!(base_param_types(&[Type::INT4, Type::TEXT]), None);
        assert_eq!(base_param_types(&[]), None);
    }

    #[test]
    fn output_position_is_the_rank_among_live_columns() {
        // attnum 2 was dropped, so attnum 3 is the second output column
        assert_eq!(output_position(&[1, 3, 4], 1), Some(0));
        assert_eq!(output_position(&[1, 3, 4], 3), Some(1));
        assert_eq!(output_position(&[1, 3, 4], 4), Some(2));
        assert_eq!(output_position(&[1, 3, 4], 2), None);
    }

    #[test]
    fn definition_statement_strips_one_trailing_semicolon() {
        assert_eq!(definition_statement(" SELECT 1;\n"), Some(" SELECT 1"));
        assert_eq!(definition_statement("SELECT 1"), Some("SELECT 1"));
    }

    #[test]
    fn definition_statement_leaves_an_inner_semicolon_to_parse() {
        assert_eq!(
            definition_statement("SELECT 1; SELECT 2;"),
            Some("SELECT 1; SELECT 2")
        );
        assert_eq!(definition_statement(";"), None);
        assert_eq!(definition_statement("  "), None);
    }

    async fn connect() -> Option<tokio_postgres::Client> {
        let Ok(dsn) = std::env::var("ROWX_TEST_DSN") else {
            assert!(
                std::env::var("ROWX_REQUIRE_DB").map_or(true, |v| v != "1"),
                "ROWX_REQUIRE_DB=1 but ROWX_TEST_DSN is not set"
            );
            return None;
        };
        let (client, connection) = tokio_postgres::connect(&dsn, tokio_postgres::NoTls)
            .await
            .ok()?;
        tokio::spawn(connection);
        Some(client)
    }

    #[tokio::test]
    async fn a_definition_with_a_second_statement_is_nullable_and_never_runs() {
        let Some(client) = connect().await else {
            return;
        };
        let mut ctx = ViewCtx::default();
        let name = "rowx_view_guard_probe";
        let crafted = format!("SELECT 1 AS a; CREATE TABLE {name} (a int);");
        assert_eq!(analyse_definition(&client, &crafted, &mut ctx).await, None);
        let exists = client
            .query_one("SELECT to_regclass($1) IS NOT NULL", &[&name])
            .await
            .map(|row| row.get::<_, bool>(0));
        assert_eq!(exists.ok(), Some(false));
    }

    // `EXPLAIN_SENT` records the text handed to the only `simple_query` the plan pass makes, so it is exactly
    // what reaches the server for the EXPLAIN. It covers the statement and the definition of a view behind it.
    #[tokio::test]
    async fn no_explain_text_contains_the_users_sql_or_a_view_definition() {
        let Some(client) = connect().await else {
            return;
        };
        let view_marker = "rowx_view_marker_77b1";
        let created = client
            .batch_execute(&format!(
                "CREATE TEMP VIEW rowx_marker_view AS SELECT '{view_marker}' AS m, c.oid \
                 FROM pg_catalog.pg_class c LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace"
            ))
            .await;
        assert!(created.is_ok());
        EXPLAIN_SENT.with(|sent| sent.borrow_mut().clear());
        let statements = [
            format!("SELECT '{MARKER}' AS a, v.oid FROM rowx_marker_view v WHERE v.oid > $1::oid"),
            format!("SELECT 1 AS \"{MARKER}\""),
            format!("SELECT $1::int4 AS a, $2::text AS \"{MARKER}\""),
        ];
        for sql in &statements {
            assert!(describe(&client, sql).await.is_ok(), "{sql}");
        }
        let sent = EXPLAIN_SENT.with(|sent| sent.borrow().clone());
        // one per statement, and one for the view's definition
        assert!(sent.len() > statements.len(), "{sent:?}");
        for text in &sent {
            assert!(text.starts_with("SET LOCAL plan_cache_mode = force_generic_plan; EXPLAIN"));
            assert!(text.contains(" EXECUTE \"s"), "{text}");
            assert!(!text.contains(MARKER), "{text}");
            assert!(!text.contains(view_marker), "{text}");
            assert!(!text.to_ascii_lowercase().contains("select"), "{text}");
        }
    }

    // a domain parameter is never explained as prepared (that would evaluate its constraints), so the statement
    // is prepared again with the base type and exactly one EXPLAIN is sent, for that statement
    #[tokio::test]
    async fn a_domain_parameter_is_explained_through_its_base_type() {
        let Some(client) = connect().await else {
            return;
        };
        let created = client
            .batch_execute("CREATE DOMAIN pg_temp.rowx_nn_oid AS oid NOT NULL")
            .await;
        assert!(created.is_ok());
        let sql = "SELECT n.nspname FROM pg_catalog.pg_class c \
                   LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace \
                   WHERE c.oid > $1::pg_temp.rowx_nn_oid";
        EXPLAIN_SENT.with(|sent| sent.borrow_mut().clear());
        let described = describe(&client, sql).await;
        assert!(described.is_ok(), "{described:?}");
        let Ok(described) = described else {
            return;
        };
        let reasons: Vec<&str> = described.columns.iter().map(|c| c.reason.name()).collect();
        assert_eq!(reasons, ["OuterJoin"]);
        let sent = EXPLAIN_SENT.with(|sent| sent.borrow().len());
        assert_eq!(sent, 1);
    }

    #[tokio::test]
    async fn a_semicolon_inside_a_literal_does_not_stop_the_analysis() {
        let Some(client) = connect().await else {
            return;
        };
        let mut ctx = ViewCtx::default();
        let definition = "SELECT ';' AS a;";
        assert_eq!(
            analyse_definition(&client, definition, &mut ctx).await,
            Some(vec![true])
        );
    }

    fn custom(name: &str, oid: u32) -> Type {
        Type::new(name.to_owned(), oid, Kind::Simple, "public".to_owned())
    }

    #[test]
    fn builtin_types_resolve_without_typsend() {
        assert_eq!(
            resolve(&Type::INT4, &HashMap::new(), &HashMap::new()),
            Ok("int4")
        );
        assert_eq!(unresolved_leaf(&Type::INT4, &HashMap::new()), None);
    }

    #[test]
    fn custom_type_with_a_builtin_send_function_is_unsupported() {
        let ty = custom("refcursor_like", 90_001);
        assert_eq!(unresolved_leaf(&ty, &HashMap::new()), Some(90_001));
        let sends = HashMap::from([(90_001, "textsend".to_owned())]);
        assert_eq!(
            resolve(&ty, &sends, &HashMap::new()),
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
        assert_eq!(
            resolve(&domain, &HashMap::new(), &HashMap::new()),
            Ok("int4")
        );
        let over_unsupported = Type::new(
            "odd_domain".to_owned(),
            90_005,
            Kind::Domain(custom("odd", 90_002)),
            "public".to_owned(),
        );
        assert!(resolve(&over_unsupported, &HashMap::new(), &HashMap::new()).is_err());
    }

    #[test]
    fn custom_type_with_other_or_no_send_function_is_unsupported() {
        let ty = custom("odd", 90_002);
        let sends = HashMap::from([(90_002, "cash_send".to_owned())]);
        assert_eq!(
            resolve(&ty, &sends, &HashMap::new()),
            Err(Unsupported {
                typsend: Some("cash_send".to_owned())
            })
        );
        assert_eq!(
            resolve(&ty, &HashMap::new(), &HashMap::new()),
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
        assert_eq!(unresolved_leaf(&array, &HashMap::new()), Some(90_002));
        assert!(resolve(&array, &HashMap::new(), &HashMap::new()).is_err());
    }

    #[test]
    fn builtin_ranges_resolve_by_subtype() {
        assert_eq!(
            resolve(&Type::INT4_RANGE, &HashMap::new(), &HashMap::new()),
            Ok("int4range")
        );
        assert_eq!(
            resolve(&Type::TSTZ_RANGE, &HashMap::new(), &HashMap::new()),
            Ok("tstzrange")
        );
        assert_eq!(unresolved_leaf(&Type::NUM_RANGE, &HashMap::new()), None);
    }

    #[test]
    fn user_defined_range_resolves_like_the_range_of_its_subtype() {
        let custom = Type::new(
            "myrange".to_owned(),
            90_010,
            Kind::Range(Type::INT4),
            "public".to_owned(),
        );
        assert_eq!(
            resolve(&custom, &HashMap::new(), &HashMap::new()),
            Ok("int4range")
        );
        assert_eq!(unresolved_leaf(&custom, &HashMap::new()), None);
    }

    #[test]
    fn range_over_an_unsupported_subtype_is_unsupported() {
        let custom = Type::new(
            "floatrange".to_owned(),
            90_011,
            Kind::Range(Type::FLOAT8),
            "public".to_owned(),
        );
        assert_eq!(unresolved_leaf(&custom, &HashMap::new()), Some(90_011));
        let sends = HashMap::from([(90_011, "range_send".to_owned())]);
        assert_eq!(
            resolve(&custom, &sends, &HashMap::new()),
            Err(Unsupported {
                typsend: Some("range_send".to_owned())
            })
        );
        // an array of the unsupported range is unsupported too
        let array = Type::new(
            "_floatrange".to_owned(),
            90_012,
            Kind::Array(custom),
            "public".to_owned(),
        );
        assert!(resolve(&array, &sends, &HashMap::new()).is_err());
    }

    #[test]
    fn array_of_ranges_resolves_to_an_array_of_the_range_key() {
        let array = Type::new(
            "_int4range".to_owned(),
            3905,
            Kind::Array(Type::INT4_RANGE),
            "pg_catalog".to_owned(),
        );
        assert_eq!(
            resolve(&array, &HashMap::new(), &HashMap::new()),
            Ok("array")
        );
        assert_eq!(
            super::element_key(&array, &HashMap::new(), &HashMap::new()),
            Some("int4range")
        );
    }

    #[test]
    fn extension_type_resolves_only_for_its_owning_extension() {
        let ty = custom("hstore", 90_020);
        assert_eq!(extension_candidates([&ty]), vec![90_020]);
        let owned = HashMap::from([(90_020, "hstore".to_owned())]);
        assert_eq!(resolve(&ty, &HashMap::new(), &owned), Ok("hstore"));
        assert_eq!(unresolved_leaf(&ty, &owned), None);
        // owned by no extension, or by another one
        assert!(resolve(&ty, &HashMap::new(), &HashMap::new()).is_err());
        let other = HashMap::from([(90_020, "ltree".to_owned())]);
        assert!(resolve(&ty, &HashMap::new(), &other).is_err());
        assert_eq!(unresolved_leaf(&ty, &other), Some(90_020));
    }

    #[test]
    fn lquery_belongs_to_the_ltree_extension() {
        let ty = custom("lquery", 90_021);
        let owned = HashMap::from([(90_021, "ltree".to_owned())]);
        assert_eq!(resolve(&ty, &HashMap::new(), &owned), Ok("lquery"));
    }

    #[test]
    fn array_of_an_extension_type_follows_the_element_owner() {
        let element = custom("citext", 90_022);
        let array = Type::new(
            "_citext".to_owned(),
            90_023,
            Kind::Array(element),
            "public".to_owned(),
        );
        assert_eq!(extension_candidates([&array]), vec![90_022]);
        let owned = HashMap::from([(90_022, "citext".to_owned())]);
        assert_eq!(
            super::element_key(&array, &HashMap::new(), &owned),
            Some("citext")
        );
        assert_eq!(
            super::element_key(&array, &HashMap::new(), &HashMap::new()),
            None
        );
    }

    #[test]
    fn impostor_names_a_same_named_type_the_extension_does_not_own() {
        let ty = custom("hstore", 90_025);
        assert_eq!(
            impostor([&ty], &HashMap::new()).as_deref(),
            Some("public.hstore")
        );
        let owned = HashMap::from([(90_025, "hstore".to_owned())]);
        assert_eq!(impostor([&ty, &Type::TEXT], &owned), None);
    }

    #[test]
    fn a_type_that_shares_no_extension_name_is_never_a_candidate() {
        assert_eq!(
            extension_candidates([&custom("mood", 90_024), &Type::TEXT]),
            Vec::<u32>::new()
        );
    }
}

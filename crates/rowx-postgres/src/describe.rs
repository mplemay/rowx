use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};

use tokio_postgres::types::{Kind, Type};
use tokio_postgres::{Client, SimpleQueryMessage};

use crate::error::Error;
use crate::explain::{self, Origin};
use crate::types::{Matcher, TYPE_TABLE};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NullReason {
    NotNullColumn,
    NullableColumn,
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
            "SELECT d.objid, e.extname::text FROM pg_depend d \
             JOIN pg_extension e ON e.oid = d.refobjid \
             WHERE d.classid = 'pg_type'::regclass AND d.refclassid = 'pg_extension'::regclass \
             AND d.deptype = 'e' AND d.objid = ANY($1::oid[])",
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
SELECT o.relid, o.attnum, n.nspname::text, c.relname::text, a.attname::text, CASE \
    WHEN c.relkind = 'v' THEN 'view' \
    WHEN c.relkind NOT IN ('r', 'p', 'm') THEN 'foreign_or_view' \
    WHEN NOT a.attnotnull THEN 'nullable' \
    WHEN EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conrelid = a.attrelid AND k.contype = 'n' \
        AND NOT k.convalidated AND k.conkey = ARRAY[a.attnum]) THEN 'not_validated' \
    ELSE 'not_null' END \
FROM unnest($1::oid[], $2::int2[]) AS o(relid, attnum) \
JOIN pg_attribute a ON a.attrelid = o.relid AND a.attnum = o.attnum \
JOIN pg_class c ON c.oid = a.attrelid \
JOIN pg_namespace n ON n.oid = c.relnamespace";

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
            "SELECT oid, typsend::text FROM pg_type WHERE oid = ANY($1::oid[])",
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
SELECT l.s, l.r, l.c FROM unnest($1::text[], $2::text[], $3::text[]) AS l(s, r, c) \
JOIN pg_namespace n ON n.nspname = l.s \
JOIN pg_class c ON c.relnamespace = n.oid AND c.relname = l.r AND c.relkind IN ('r', 'p') \
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = l.c AND a.attnotnull AND NOT a.attisdropped \
WHERE NOT EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conrelid = a.attrelid AND k.contype = 'n' \
    AND NOT k.convalidated AND k.conkey = ARRAY[a.attnum])";

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
SELECT 1 FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace \
WHERE p.proname = ANY($1::text[]) AND n.nspname <> 'pg_catalog' LIMIT 1";

async fn functions_are_unshadowed(client: &Client) -> bool {
    let names: Vec<&str> = explain::WHITELISTED_FUNCTIONS.to_vec();
    client
        .query(SHADOWED_FUNCTIONS_SQL, &[&names])
        .await
        .is_ok_and(|rows| rows.is_empty())
}

// the promotions to Nullable, per column whether a set operation proves it NOT NULL, and per column whether its
// computed expression does
type PlanVerdicts = (Vec<Option<NullReason>>, Vec<bool>, Vec<bool>);

// simple_query, because the extended protocol would demand values for `$n`; the caller's Parse has
// already rejected anything but a single statement
async fn plan_promotions(
    client: &Client,
    sql: &str,
    column_count: usize,
    origins: &[Option<Origin>],
) -> Option<PlanVerdicts> {
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
    let leaves = not_null_leaves(client, &explain::leaf_columns(&plan)).await;
    // the lookup only runs when a function name is what proves a column
    let functions = !explain::needs_function_check(&plan, column_count)
        || functions_are_unshadowed(client).await;
    Some((
        explain::walk(&plan, column_count, origins),
        explain::proven_not_null(&plan, column_count, &leaves),
        explain::proven_computed(&plan, column_count, functions),
    ))
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
// the plan pass ran; when it did not, a view definition is not trusted.
async fn refine(
    client: &Client,
    sql: &str,
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
    if let Some((promotions, proven, computed)) =
        plan_promotions(client, sql, slots.len(), &column_origins).await
    {
        for (((slot, promotion), proven), computed) in
            slots.iter_mut().zip(promotions).zip(proven).zip(computed)
        {
            if slot.reason == NullReason::Computed && (proven || computed) {
                slot.nullable = false;
                slot.reason = if computed {
                    NullReason::ComputedExpr
                } else {
                    NullReason::SetOperation
                };
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

// The definition without its trailing `;`, because it is concatenated into a simple-protocol `EXPLAIN`. That is
// only safe once Parse has accepted the text as a single statement (`analyse_definition` prepares it first).
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
             WHERE attrelid = $1::oid AND attnum > 0 AND NOT attisdropped ORDER BY attnum",
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
SELECT c.relkind = 'v' \
    AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_rewrite r WHERE r.ev_class = c.oid AND r.rulename <> '_RETURN') \
    AND (NOT COALESCE((SELECT bool_or(o.option_value::boolean) FROM pg_catalog.pg_options_to_table(c.reloptions) o \
            WHERE o.option_name = 'security_invoker'), false) \
        OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_rewrite r \
            JOIN pg_catalog.pg_depend d ON d.classid = 'pg_catalog.pg_rewrite'::regclass AND d.objid = r.oid \
                AND d.refclassid = 'pg_catalog.pg_class'::regclass AND d.refobjid <> c.oid \
            WHERE r.ev_class = c.oid AND NOT pg_catalog.has_table_privilege(d.refobjid, 'SELECT'))) \
FROM pg_catalog.pg_class c WHERE c.oid = $1::oid";

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
        .query_one("SELECT pg_catalog.pg_get_viewdef($1::oid, false)", &[&view])
        .await
        .ok()?
        .try_get(0)
        .ok()?;
    analyse_definition(client, &definition, ctx).await
}

// Parse rejects anything but a single statement, as for the user's own query, and only then is the text
// concatenated into `EXPLAIN`; a definition that fails Parse, or carries a second statement, is Nullable
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
    let plan_ran = Box::pin(refine(client, sql, &mut slots, ctx)).await.ok()?;
    plan_ran.then(|| slots.iter().map(|s| !s.nullable).collect())
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
    refine(client, sql, &mut slots, &mut ViewCtx::default()).await?;
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
        Unsupported, ViewCtx, analyse_definition, definition_statement, extension_candidates,
        impostor, output_position, resolve, unresolved_leaf,
    };

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

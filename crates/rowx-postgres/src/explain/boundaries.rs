use std::collections::{HashMap, HashSet};

use serde_json::Value;

use super::{Marked, Origin, children, null_extends, outputs, str_key};

pub(super) type Ctes<'a> = HashMap<&'a str, &'a Value>;

pub(super) fn collect_ctes<'a>(node: &'a Value, ctes: &mut Ctes<'a>) {
    if let Some(name) = str_key(node, "Subplan Name").and_then(|n| n.strip_prefix("CTE ")) {
        ctes.insert(name, node);
    }
    for child in children(node) {
        collect_ctes(child, ctes);
    }
}

pub(super) fn has_multiple_grouping_sets(node: &Value) -> bool {
    // a rollup chain is one entry whose "Group Keys" lists several sets, so count the sets, not the entries
    node.get("Grouping Sets")
        .and_then(Value::as_array)
        .is_some_and(|entries| {
            let sets: usize = entries
                .iter()
                .flat_map(|entry| ["Group Keys", "Hash Keys"].map(|key| entry.get(key)))
                .flatten()
                .filter_map(Value::as_array)
                .map(Vec::len)
                .sum();
            sets > 1 || entries.len() > 1
        })
}

// True when anything under `node` can introduce NULLs: a null-extending join or a grouping-sets aggregate.
fn introduces_nulls<'a>(node: &'a Value, ctes: &Ctes<'a>, seen: &mut HashSet<&'a str>) -> bool {
    if has_multiple_grouping_sets(node) {
        return true;
    }
    let join_type = str_key(node, "Join Type");
    for child in children(node) {
        if null_extends(join_type, child) || introduces_nulls(child, ctes, seen) {
            return true;
        }
    }
    // a scan of another CTE inherits whatever that CTE introduces; `seen` stops recursive CTEs looping
    cte_name(node).is_some_and(|name| {
        seen.insert(name)
            && ctes
                .get(name)
                .is_some_and(|source| introduces_nulls(source, ctes, seen))
    })
}

fn cte_name(node: &Value) -> Option<&str> {
    match str_key(node, "Node Type")? {
        "CTE Scan" | "WorkTable Scan" => {
            str_key(node, "CTE Name").or_else(|| str_key(node, "Alias"))
        }
        _ => None,
    }
}

// Aliases of every Subquery Scan / CTE Scan / WorkTable Scan whose source can introduce NULLs.
pub(super) fn nullable_aliases<'a>(node: &'a Value, ctes: &Ctes<'a>, found: &mut Vec<&'a str>) {
    if let (Some("Subquery Scan" | "CTE Scan" | "WorkTable Scan"), Some(alias)) =
        (str_key(node, "Node Type"), str_key(node, "Alias"))
    {
        let nullable = cte_name(node).map_or_else(
            || introduces_nulls(node, ctes, &mut HashSet::new()),
            |name| {
                ctes.get(name).is_some_and(|source| {
                    introduces_nulls(source, ctes, &mut HashSet::from([name]))
                })
            },
        );
        if nullable {
            found.push(alias);
        }
    }
    for child in children(node) {
        nullable_aliases(child, ctes, found);
    }
}

fn qualifier(expr: &str) -> Option<&str> {
    let (head, _) = expr.split_once('.')?;
    head.chars()
        .all(|c| c.is_alphanumeric() || c == '_')
        .then_some(head)
}

// Per-output promotion for a ModifyTable root. The plan doesn't record `WITH (OLD AS o, NEW AS n)`,
// so any other qualifier is treated as a possible alias, which only over-promotes.
pub(super) fn returning_promotions(root: &Value) -> Vec<bool> {
    let operation = str_key(root, "Operation");
    let exprs: Vec<&str> = outputs(root).collect();
    let target = [str_key(root, "Alias"), str_key(root, "Relation Name")];
    let is_alias = |q: &str| !matches!(q, "old" | "new") && !target.contains(&Some(q));
    let aliased = exprs.iter().any(|e| qualifier(e).is_some_and(is_alias));

    exprs
        .iter()
        .map(|expr| {
            let qual = qualifier(expr);
            match operation {
                Some("Merge") if aliased => true,
                Some("Merge") => matches!(qual, Some("old" | "new")),
                Some("Insert") => qual.is_some_and(|q| q == "old" || is_alias(q)),
                Some("Delete") => qual.is_some_and(|q| q == "new" || is_alias(q)),
                _ => false,
            }
        })
        .collect()
}

// resolving one boundary output to its source: a chain of boundaries is walked one at a time and
// stops at a relation scan, which is where the origin identity is checked
const MAX_DEPTH: usize = 16;

// A node that merges several inputs under the first input's column names, so an output entry says
// nothing about which input a row came from.
fn merges_inputs(node: &Value) -> bool {
    matches!(
        str_key(node, "Node Type"),
        Some("Append" | "Merge Append" | "SetOp" | "Recursive Union")
    ) || children(node).any(merges_inputs)
}

fn scans_with_alias<'a>(node: &'a Value, alias: &str, found: &mut Vec<&'a Value>) {
    if str_key(node, "Alias") == Some(alias)
        && str_key(node, "Node Type").is_some_and(|t| t.ends_with(" Scan"))
    {
        found.push(node);
    }
    for child in children(node) {
        scans_with_alias(child, alias, found);
    }
}

fn unique_scan<'a>(subtree: &'a Value, alias: &str) -> Option<&'a Value> {
    let mut found = Vec::new();
    scans_with_alias(subtree, alias, &mut found);
    match found.as_slice() {
        [only] => Some(only),
        _ => None,
    }
}

// `rel.col` where both parts are plain unquoted identifiers
fn plain_ref(entry: &str) -> Option<(&str, &str)> {
    let plain = |part: &str| {
        part.chars()
            .next()
            .is_some_and(|c| c.is_ascii_lowercase() || c == '_')
            && part
                .chars()
                .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
    };
    let (rel, col) = entry.split_once('.')?;
    (plain(rel) && plain(col)).then_some((rel, col))
}

// The node a boundary reads from: its only child, or the InitPlan of its CTE.
fn source_of<'a>(boundary: &'a Value, ctes: &Ctes<'a>) -> Option<&'a Value> {
    match str_key(boundary, "Node Type")? {
        "Subquery Scan" => match children(boundary).collect::<Vec<_>>().as_slice() {
            [only] => Some(only),
            _ => None,
        },
        "CTE Scan" => ctes.get(str_key(boundary, "CTE Name")?).copied(),
        _ => None,
    }
}

// True when output `col` of `scan` is proven to be `origin`'s column and never NULL-extended on the way.
fn column_is_clean<'a>(
    scan: &'a Value,
    col: &str,
    origin: &Origin,
    ctx: &Context<'a, '_>,
    depth: usize,
) -> bool {
    if depth > MAX_DEPTH {
        return false;
    }
    if let Some(relation) = str_key(scan, "Relation Name") {
        return str_key(scan, "Schema") == Some(origin.schema.as_str())
            && relation == origin.relation
            && col == origin.column;
    }
    let Some(source) = source_of(scan, ctx.ctes) else {
        return false;
    };
    if merges_inputs(source) {
        return false;
    }
    // step 1: exactly one entry bears the name, step 2: it is a plain reference
    let named: Vec<&str> = outputs(source)
        .filter(|entry| entry.rsplit('.').next() == Some(col))
        .collect();
    let [entry] = named.as_slice() else {
        return false;
    };
    let Some((rel, entry_col)) = plain_ref(entry) else {
        return false;
    };
    // step 3: the ordinary walk did not mark it
    if ctx.marked.contains_key(*entry) {
        return false;
    }
    // step 4 happens at the relation scan this reference resolves to
    unique_scan(source, rel)
        .is_some_and(|next| column_is_clean(next, entry_col, origin, ctx, depth + 1))
}

pub(super) struct Context<'a, 'm> {
    pub(super) root: &'a Value,
    pub(super) ctes: &'m Ctes<'a>,
    pub(super) marked: &'m Marked,
}

// True when the top output `expr` (`x.col`) may keep the verdict the catalog gave it.
pub(super) fn boundary_output_is_clean(expr: &str, origin: Option<&Origin>, ctx: &Context) -> bool {
    let (Some(origin), Some((alias, col))) = (origin, plain_ref(expr)) else {
        return false;
    };
    unique_scan(ctx.root, alias).is_some_and(|scan| column_is_clean(scan, col, origin, ctx, 0))
}

use std::collections::HashSet;

use serde_json::Value;

use super::{Marked, Origin, children, mark_joins, outputs, str_key};

/// The base columns the plan proves NOT NULL, looked up in the catalog by the caller.
pub type NotNullLeaves = HashSet<Origin>;

const SCANS: [&str; 6] = [
    "Seq Scan",
    "Sample Scan",
    "Index Scan",
    "Index Only Scan",
    "Bitmap Heap Scan",
    "Tid Scan",
];

// nodes that hand their child's rows through unchanged when their own `Output` equals the child's
const PASS_THROUGH: [&str; 4] = ["Aggregate", "Unique", "Sort", "Limit"];

fn has_merge_node(node: &Value) -> bool {
    matches!(
        str_key(node, "Node Type"),
        Some("Append" | "Merge Append" | "SetOp")
    ) || children(node).any(has_merge_node)
}

fn collect_leaves(node: &Value, found: &mut Vec<Origin>) {
    if let (Some(schema), Some(relation)) =
        (str_key(node, "Schema"), str_key(node, "Relation Name"))
        && let Some(alias) = str_key(node, "Alias")
    {
        for expr in outputs(node) {
            if let Some(column) = plain_column(expr, alias) {
                found.push(Origin {
                    schema: schema.to_owned(),
                    relation: relation.to_owned(),
                    column: column.to_owned(),
                });
            }
        }
    }
    for child in children(node) {
        collect_leaves(child, found);
    }
}

/// Every plain base-table column the plan scans. Empty unless the plan has a set operation, so a query
/// without one costs no catalog round trip.
#[must_use]
pub fn leaf_columns(plan: &Value) -> Vec<Origin> {
    let mut found = Vec::new();
    if let Some(root) = plan.get(0).and_then(|entry| entry.get("Plan"))
        && has_merge_node(root)
    {
        collect_leaves(root, &mut found);
    }
    found.sort();
    found.dedup();
    found
}

// only a plain unquoted lowercase identifier is trusted to be the attribute name
fn is_plain_ident(text: &str) -> bool {
    let mut chars = text.chars();
    chars
        .next()
        .is_some_and(|c| c.is_ascii_lowercase() || c == '_')
        && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
}

fn plain_column<'a>(expr: &'a str, alias: &str) -> Option<&'a str> {
    if !is_plain_ident(alias) {
        return None;
    }
    let column = expr.strip_prefix(alias)?.strip_prefix('.')?;
    is_plain_ident(column).then_some(column)
}

/// Per top-level column: whether the plan proves the set operation behind it is never NULL.
#[must_use]
pub fn proven_not_null(plan: &Value, column_count: usize, leaves: &NotNullLeaves) -> Vec<bool> {
    let Some(root) = plan.get(0).and_then(|entry| entry.get("Plan")) else {
        return vec![false; column_count];
    };
    if leaves.is_empty() || !has_merge_node(root) {
        return vec![false; column_count];
    }
    let mut marked = Marked::new();
    mark_joins(root, None, &mut marked);
    let context = Context {
        marked: &marked,
        leaves,
    };
    (0..column_count)
        .map(|position| not_null_at(root, position, &context))
        .collect()
}

struct Context<'a> {
    marked: &'a Marked,
    leaves: &'a NotNullLeaves,
}

// Append and Merge Append print no `Output`, and take their first child's
fn effective_outputs(node: &Value) -> Vec<&str> {
    if node.get("Output").is_some() {
        return outputs(node).collect();
    }
    if matches!(str_key(node, "Node Type"), Some("Append" | "Merge Append"))
        && let Some(first) = children(node).next()
    {
        return effective_outputs(first);
    }
    Vec::new()
}

fn not_null_at(node: &Value, position: usize, context: &Context) -> bool {
    let Some(node_type) = str_key(node, "Node Type") else {
        return false;
    };
    match node_type {
        "Append" | "Merge Append" => {
            let mut any = false;
            for child in children(node) {
                any = true;
                if !not_null_at(child, position, context) {
                    return false;
                }
            }
            any
        }
        "SetOp" => {
            let side = |relationship: &str| {
                children(node)
                    .find(|child| str_key(child, "Parent Relationship") == Some(relationship))
                    .is_some_and(|child| not_null_at(child, position, context))
            };
            match str_key(node, "Command") {
                Some("Except" | "Except All") => side("Outer"),
                Some("Intersect" | "Intersect All") => side("Outer") || side("Inner"),
                _ => false,
            }
        }
        _ if PASS_THROUGH.contains(&node_type) => {
            let mut kids = children(node);
            match (kids.next(), kids.next()) {
                (Some(child), None) => {
                    let own = effective_outputs(node);
                    !own.is_empty()
                        && own == effective_outputs(child)
                        && not_null_at(child, position, context)
                }
                _ => false,
            }
        }
        _ if SCANS.contains(&node_type) => leaf_not_null(node, position, context),
        _ => false,
    }
}

fn leaf_not_null(node: &Value, position: usize, context: &Context) -> bool {
    let (Some(schema), Some(relation), Some(alias)) = (
        str_key(node, "Schema"),
        str_key(node, "Relation Name"),
        str_key(node, "Alias"),
    ) else {
        return false;
    };
    let Some(expr) = outputs(node).nth(position) else {
        return false;
    };
    let Some(column) = plain_column(expr, alias) else {
        return false;
    };
    !context.marked.contains_key(expr)
        && context.leaves.contains(&Origin {
            schema: schema.to_owned(),
            relation: relation.to_owned(),
            column: column.to_owned(),
        })
}

#[cfg(test)]
mod tests {
    use serde_json::{Value, json};

    use super::{NotNullLeaves, Origin, leaf_columns, proven_not_null};

    fn scan(relation: &str, column: &str, relationship: &str) -> Value {
        json!({
            "Node Type": "Seq Scan", "Parent Relationship": relationship,
            "Schema": "s", "Relation Name": relation, "Alias": relation,
            "Output": [format!("{relation}.{column}")],
        })
    }

    fn plan(root: &Value) -> Value {
        json!([{ "Plan": root }])
    }

    fn leaves(pairs: &[(&str, &str)]) -> NotNullLeaves {
        pairs
            .iter()
            .map(|(relation, column)| Origin {
                schema: "s".to_owned(),
                relation: (*relation).to_owned(),
                column: (*column).to_owned(),
            })
            .collect()
    }

    fn append(children: &[Value]) -> Value {
        json!({ "Node Type": "Append", "Parent Relationship": "Outer", "Plans": children })
    }

    fn setop(command: &str, left: Value, right: Value) -> Value {
        let mut left = left;
        let mut right = right;
        left["Parent Relationship"] = json!("Outer");
        right["Parent Relationship"] = json!("Inner");
        json!({ "Node Type": "SetOp", "Command": command, "Output": ["a.x"], "Plans": [left, right] })
    }

    fn proven(root: &Value, known: &[(&str, &str)]) -> bool {
        proven_not_null(&plan(root), 1, &leaves(known))[0]
    }

    #[test]
    fn append_needs_every_branch() {
        let root = append(&[scan("a", "x", "Member"), scan("b", "y", "Member")]);
        assert!(proven(&root, &[("a", "x"), ("b", "y")]));
        assert!(!proven(&root, &[("a", "x")]));
    }

    #[test]
    fn empty_append_is_nullable() {
        assert!(!proven(&append(&[]), &[("a", "x")]));
    }

    #[test]
    fn except_follows_the_left_branch() {
        let root = setop("Except", scan("a", "x", ""), scan("b", "y", ""));
        assert!(proven(&root, &[("a", "x")]));
        assert!(!proven(&root, &[("b", "y")]));
    }

    #[test]
    fn intersect_follows_either_branch() {
        let root = setop("Intersect", scan("a", "x", ""), scan("b", "y", ""));
        assert!(proven(&root, &[("a", "x")]));
        assert!(proven(&root, &[("b", "y")]));
        assert!(!proven(&root, &[]));
    }

    #[test]
    fn pass_through_needs_equal_outputs() {
        let sort = |output: &str| {
            json!({ "Node Type": "Sort", "Output": [output],
                "Plans": [append(&[scan("a", "x", "Member")])] })
        };
        assert!(proven(&sort("a.x"), &[("a", "x")]));
        assert!(!proven(&sort("upper(a.x)"), &[("a", "x")]));
    }

    #[test]
    fn constant_branch_is_nullable() {
        let null = json!({ "Node Type": "Result", "Parent Relationship": "Member", "Output": ["NULL::text"] });
        assert!(!proven(
            &append(&[scan("a", "x", "Member"), null]),
            &[("a", "x")]
        ));
    }

    #[test]
    fn quoted_names_are_not_trusted() {
        let quoted = json!({
            "Node Type": "Seq Scan", "Schema": "s", "Relation Name": "a", "Alias": "a",
            "Output": ["a.\"X\""],
        });
        assert!(!proven(&append(&[quoted]), &[("a", "\"X\"")]));
    }

    #[test]
    fn null_extended_leaf_is_nullable() {
        let join = json!({
            "Node Type": "Hash Join", "Join Type": "Left", "Output": ["a.x"],
            "Plans": [scan("a", "x", "Outer"), scan("b", "y", "Inner")],
        });
        // the join is below a set operation only through a branch, which is not followed, so build it directly
        let root = append(&[join]);
        assert!(!proven(&root, &[("a", "x"), ("b", "y")]));
    }

    #[test]
    fn leaves_are_collected_only_for_set_operations() {
        let plain = plan(&scan("a", "x", ""));
        assert_eq!(leaf_columns(&plain).len(), 0);
        let union = plan(&append(&[scan("a", "x", "Member")]));
        assert_eq!(leaf_columns(&union).len(), 1);
    }
}

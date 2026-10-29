use std::collections::HashMap;

use serde_json::Value;

use crate::describe::NullReason;

mod boundaries;

type Marked = HashMap<String, NullReason>;

/// The base column a top-level column was traced to by describe.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Origin {
    pub schema: String,
    pub relation: String,
    pub column: String,
}

// Returns one entry per top-level column: `Some` is a promotion to Nullable, `None` means no opinion.
#[must_use]
pub fn walk(
    plan: &Value,
    column_count: usize,
    origins: &[Option<Origin>],
) -> Vec<Option<NullReason>> {
    let mut promotions = vec![None; column_count];
    let Some(root) = plan.get(0).and_then(|entry| entry.get("Plan")) else {
        return promotions;
    };

    let mut marked = Marked::new();
    mark_joins(root, None, &mut marked);

    let mut ctes = boundaries::Ctes::new();
    boundaries::collect_ctes(root, &mut ctes);
    let mut aliases = Vec::new();
    boundaries::nullable_aliases(root, &ctes, &mut aliases);

    let returning = if str_key(root, "Node Type") == Some("ModifyTable") {
        boundaries::returning_promotions(root)
    } else {
        Vec::new()
    };

    let context = boundaries::Context {
        root,
        ctes: &ctes,
        marked: &marked,
    };

    for (index, (slot, expr)) in promotions.iter_mut().zip(outputs(root)).enumerate() {
        *slot = marked.get(expr).cloned().or_else(|| {
            aliases
                .iter()
                .find(|alias| {
                    expr.strip_prefix(**alias)
                        .is_some_and(|rest| rest.starts_with('.'))
                })
                .filter(|_| {
                    let origin = origins.get(index).and_then(Option::as_ref);
                    !boundaries::boundary_output_is_clean(expr, origin, &context)
                })
                .map(|alias| NullReason::ScanBoundary {
                    alias: (*alias).to_owned(),
                })
                .or_else(|| {
                    returning
                        .get(index)
                        .copied()
                        .unwrap_or(false)
                        .then_some(NullReason::ReturningOldNew)
                })
        });
    }
    promotions
}

fn children(node: &Value) -> impl Iterator<Item = &Value> {
    node.get("Plans")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
}

fn null_extends(join_type: Option<&str>, child: &Value) -> bool {
    matches!(
        (join_type, str_key(child, "Parent Relationship")),
        (Some("Full"), _)
            | (Some("Left" | "Anti"), Some("Inner"))
            | (Some("Right" | "Right Anti"), Some("Outer"))
    )
}

fn outputs(node: &Value) -> impl Iterator<Item = &str> {
    node.get("Output")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
}

fn str_key<'a>(node: &'a Value, key: &str) -> Option<&'a str> {
    node.get(key).and_then(Value::as_str)
}

// `inherited` is set when an ancestor join null-extends this whole subtree; it stays set all the way down.
fn mark_joins(node: &Value, inherited: Option<&NullReason>, marked: &mut Marked) {
    if let Some(reason) = inherited {
        for expr in outputs(node) {
            marked
                .entry(expr.to_owned())
                .or_insert_with(|| reason.clone());
        }
    }

    let join_type = str_key(node, "Join Type");
    let reason = NullReason::OuterJoin {
        node: str_key(node, "Node Type").unwrap_or_default().to_owned(),
    };

    if boundaries::has_multiple_grouping_sets(node) {
        for expr in outputs(node) {
            marked
                .entry(expr.to_owned())
                .or_insert(NullReason::GroupingSets);
        }
    }

    for child in children(node) {
        let null_extended = null_extends(join_type, child).then_some(&reason);
        mark_joins(child, null_extended.or(inherited), marked);
    }
}

#[cfg(test)]
mod join {
    use std::fs;
    use std::path::PathBuf;

    use proptest::prelude::*;
    use serde_json::{Value, json};

    use super::{NullReason, Origin, walk};

    fn fixture(name: &str) -> Value {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("tests/fixtures/plans")
            .join(format!("{name}.json"));
        let text = fs::read_to_string(&path).unwrap_or_default();
        // the first line is the `-- server_version` sidecar comment
        let body: String = text
            .lines()
            .filter(|l| !l.starts_with("--"))
            .collect::<Vec<_>>()
            .join("\n");
        serde_json::from_str(&body).unwrap_or(Value::Null)
    }

    fn nullable_flags(name: &str, columns: usize) -> Vec<bool> {
        walk(&fixture(name), columns, &[])
            .iter()
            .map(Option::is_some)
            .collect()
    }

    #[test]
    fn left_where_param_marks_inner_side() {
        assert_eq!(nullable_flags("join_left_where_param", 2), [false, true]);
    }

    #[test]
    fn right_join_marks_outer_side() {
        assert_eq!(nullable_flags("join_right", 2), [false, true]);
    }

    #[test]
    fn left_order_by_descends_through_sort() {
        assert_eq!(nullable_flags("join_left_order_by", 2), [false, true]);
    }

    #[test]
    fn left_trivial_subquery_is_nullable() {
        assert_eq!(nullable_flags("join_left_trivial_subquery", 1), [true]);
    }

    #[test]
    fn duplicate_columns_are_each_promoted() {
        assert_eq!(nullable_flags("join_duplicate_columns", 2), [true, true]);
    }

    #[test]
    fn full_join_marks_both_sides() {
        assert_eq!(nullable_flags("join_full", 2), [true, true]);
    }

    #[test]
    fn lateral_marks_through_limit() {
        assert_eq!(nullable_flags("join_lateral", 2), [false, true]);
    }

    #[test]
    fn left_reduced_to_inner_promotes_nothing() {
        assert_eq!(
            nullable_flags("join_left_reduced_to_inner", 2),
            [false, false]
        );
    }

    #[test]
    fn anti_join_does_not_null_extend() {
        assert_eq!(nullable_flags("join_anti", 1), [false]);
    }

    // the executor feeds a null tuple for the inner side of an anti join, so its columns are NULL in the output
    #[test]
    fn anti_join_null_extends_the_inner_columns_it_outputs() {
        assert_eq!(nullable_flags("join_anti_null_extended", 1), [true]);
    }

    // a sorted rollup chain is one "Grouping Sets" entry that lists every set under "Group Keys"
    #[test]
    fn sorted_cube_chain_marks_grouping_sets() {
        assert_eq!(
            reasons("grouping_cube_chain", 2),
            [
                Some(NullReason::GroupingSets),
                Some(NullReason::GroupingSets)
            ]
        );
    }

    #[test]
    fn semi_join_does_not_null_extend() {
        assert_eq!(nullable_flags("join_semi", 1), [false]);
    }

    #[test]
    fn nested_subquery_promotes_nothing() {
        assert_eq!(nullable_flags("join_nested_subquery", 2), [false, false]);
    }

    #[test]
    fn reason_names_the_join_node() {
        assert_eq!(
            walk(&fixture("join_right"), 2, &[])[1],
            Some(NullReason::OuterJoin {
                node: "Hash Join".to_owned()
            }),
        );
    }

    fn reasons(name: &str, columns: usize) -> Vec<Option<NullReason>> {
        walk(&fixture(name), columns, &[])
    }

    fn boundary(alias: &str) -> NullReason {
        NullReason::ScanBoundary {
            alias: alias.to_owned(),
        }
    }

    #[test]
    fn window_function_subquery_is_nullable() {
        assert_eq!(nullable_flags("window_subquery", 2), [true, false]);
    }

    #[test]
    fn materialized_cte_scan_is_a_boundary() {
        assert_eq!(reasons("cte_materialized", 1), [Some(boundary("c"))]);
    }

    #[test]
    fn recursive_cte_without_nulls_promotes_nothing() {
        assert!(reasons("cte_recursive", 1).iter().all(Option::is_none));
    }

    #[test]
    fn rollup_marks_grouping_sets() {
        assert_eq!(
            reasons("grouping_rollup", 1),
            [Some(NullReason::GroupingSets)]
        );
    }

    #[test]
    fn insert_returning_old_is_promoted_not_new() {
        assert_eq!(
            reasons("returning_insert_old", 2),
            [Some(NullReason::ReturningOldNew), None]
        );
    }

    #[test]
    fn delete_returning_new_is_promoted() {
        assert_eq!(
            reasons("returning_delete_new", 1),
            [Some(NullReason::ReturningOldNew)]
        );
    }

    #[test]
    fn update_returning_old_new_promotes_nothing() {
        assert_eq!(
            nullable_flags("returning_update_old_new", 2),
            [false, false]
        );
    }

    #[test]
    fn plain_returning_promotes_nothing() {
        assert_eq!(nullable_flags("returning_plain", 3), [false, false, false]);
    }

    #[test]
    fn merge_promotes_old_and_new_but_not_the_action() {
        assert_eq!(nullable_flags("merge_returning", 3), [false, true, true]);
    }

    #[test]
    fn aliased_update_promotes_nothing() {
        assert_eq!(nullable_flags("returning_alias", 2), [false, false]);
    }

    fn origin(relation: &str, column: &str) -> Origin {
        Origin {
            schema: "public".to_owned(),
            relation: relation.to_owned(),
            column: column.to_owned(),
        }
    }

    fn mapped(name: &str, origins: &[Option<Origin>]) -> Vec<Option<NullReason>> {
        walk(&fixture(name), origins.len(), origins)
    }

    #[test]
    fn subquery_keeps_the_non_null_side_not_promoted() {
        assert_eq!(
            mapped(
                "scan_boundary_subquery",
                &[Some(origin("users", "id")), Some(origin("posts", "title"))]
            ),
            [None, Some(boundary("x"))]
        );
    }

    #[test]
    fn materialized_cte_keeps_the_non_null_side_not_promoted() {
        assert_eq!(
            mapped(
                "scan_boundary_cte_materialized",
                &[Some(origin("users", "id")), Some(origin("posts", "title"))]
            ),
            [None, Some(boundary("c"))]
        );
    }

    #[test]
    fn cte_chain_resolves_one_boundary_at_a_time() {
        assert_eq!(
            mapped(
                "scan_boundary_cte_chain",
                &[Some(origin("users", "id")), Some(origin("posts", "title"))]
            ),
            [None, Some(boundary("b"))]
        );
    }

    #[test]
    fn cte_referenced_twice_is_mapped_per_alias() {
        assert_eq!(
            mapped(
                "scan_boundary_cte_twice",
                &[Some(origin("users", "id")), Some(origin("posts", "title"))]
            ),
            [None, Some(boundary("b"))]
        );
    }

    #[test]
    fn expression_output_is_promoted() {
        assert_eq!(
            mapped(
                "scan_boundary_expression",
                &[Some(origin("users", "id")), None]
            ),
            [None, Some(boundary("x"))]
        );
    }

    #[test]
    fn same_named_source_columns_are_promoted() {
        assert_eq!(
            mapped(
                "scan_boundary_duplicate_names",
                &[Some(origin("users", "id")), Some(origin("posts", "id"))]
            ),
            [Some(boundary("x")), Some(boundary("x"))]
        );
    }

    // the window function is gone from the plan, so the source output list ends in a NULL constant
    #[test]
    fn planner_null_constant_does_not_disturb_the_mapping() {
        assert_eq!(
            mapped(
                "scan_boundary_null_constant",
                &[Some(origin("users", "id"))]
            ),
            [None]
        );
    }

    #[test]
    fn grouping_sets_source_is_promoted() {
        assert_eq!(
            mapped(
                "scan_boundary_grouping_sets",
                &[
                    Some(origin("users", "name")),
                    Some(origin("users", "email"))
                ]
            ),
            [Some(boundary("x")), Some(boundary("x"))]
        );
    }

    #[test]
    fn swapped_aliases_are_promoted() {
        assert_eq!(
            mapped(
                "scan_boundary_swapped",
                &[
                    Some(origin("posts", "title")),
                    Some(origin("users", "name"))
                ]
            ),
            [Some(boundary("c")), Some(boundary("c"))]
        );
    }

    #[test]
    fn missing_origin_is_promoted() {
        assert_eq!(
            mapped("scan_boundary_subquery", &[None, None]),
            [Some(boundary("x")), Some(boundary("x"))]
        );
    }

    #[test]
    fn origin_in_another_schema_is_promoted() {
        let mut other = origin("users", "id");
        "elsewhere".clone_into(&mut other.schema);
        assert_eq!(
            mapped(
                "scan_boundary_subquery",
                &[Some(other), Some(origin("posts", "title"))]
            ),
            [Some(boundary("x")), Some(boundary("x"))]
        );
    }

    // an Append names its output after the first branch, so a NULL from another branch is invisible
    #[test]
    fn append_source_is_promoted() {
        assert_eq!(
            mapped("scan_boundary_union_all", &[Some(origin("posts", "title"))]),
            [Some(boundary("x"))]
        );
    }

    #[test]
    fn malformed_plan_promotes_nothing() {
        assert_eq!(walk(&json!({}), 3, &[]), [None, None, None]);
    }

    fn arb_node() -> impl Strategy<Value = Value> {
        let leaf = prop::collection::vec("[a-c]\\.[xyz]", 0..4)
            .prop_map(|out| json!({ "Node Type": "Seq Scan", "Output": out }));
        leaf.prop_recursive(4, 24, 3, |inner| {
            (
                prop::sample::select(vec!["Full", "Left", "Right", "Inner", "Semi", "Anti", "Right Anti"]),
                prop::collection::vec("[a-c]\\.[xyz]", 0..4),
                prop::collection::vec((inner, prop::sample::select(vec!["Outer", "Inner", "SubPlan"])), 0..3),
            )
                .prop_map(|(join, out, kids)| {
                    let plans: Vec<Value> = kids
                        .into_iter()
                        .map(|(mut kid, rel)| {
                            kid["Parent Relationship"] = json!(rel);
                            kid
                        })
                        .collect();
                    json!({ "Node Type": "Nested Loop", "Join Type": join, "Output": out, "Plans": plans })
                })
        })
    }

    proptest! {
        // The walker only ever yields promotions; a plan with no null-extending join yields none.
        #[test]
        fn only_promotions(root in arb_node(), columns in 0usize..6) {
            let result = walk(&json!([{ "Plan": root }]), columns, &[]);
            prop_assert_eq!(result.len(), columns);
            for promotion in result.into_iter().flatten() {
                let is_outer_join = matches!(promotion, NullReason::OuterJoin { .. });
                prop_assert!(is_outer_join);
            }
        }

        #[test]
        fn no_outer_join_no_promotion(out in prop::collection::vec("[a-c]\\.[xyz]", 0..4), columns in 0usize..6) {
            let plan = json!([{ "Plan": { "Node Type": "Seq Scan", "Output": out } }]);
            prop_assert!(walk(&plan, columns, &[]).iter().all(Option::is_none));
        }
    }
}

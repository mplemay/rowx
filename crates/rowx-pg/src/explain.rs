use std::collections::HashMap;

use serde_json::Value;

// Mirrors design §8.1. T31 moves this to describe.rs; only the plan-walk variants are produced here.
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

type Marked = HashMap<String, NullReason>;

// Returns one entry per top-level column: `Some` is a promotion to Nullable, `None` means no opinion.
#[must_use]
pub fn walk(plan: &Value, column_count: usize) -> Vec<Option<NullReason>> {
    let mut promotions = vec![None; column_count];
    let Some(root) = plan.get(0).and_then(|entry| entry.get("Plan")) else {
        return promotions;
    };

    let mut marked = Marked::new();
    mark_joins(root, None, &mut marked);

    for (slot, expr) in promotions.iter_mut().zip(outputs(root)) {
        *slot = marked.get(expr).cloned();
    }
    promotions
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

    let children = node
        .get("Plans")
        .and_then(Value::as_array)
        .into_iter()
        .flatten();
    for child in children {
        let null_extended = match (join_type, str_key(child, "Parent Relationship")) {
            (Some("Full"), _) | (Some("Left"), Some("Inner")) | (Some("Right"), Some("Outer")) => {
                Some(&reason)
            }
            _ => None,
        };
        mark_joins(child, null_extended.or(inherited), marked);
    }
}

#[cfg(test)]
mod join {
    use std::fs;
    use std::path::PathBuf;

    use proptest::prelude::*;
    use serde_json::{Value, json};

    use super::{NullReason, walk};

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
        walk(&fixture(name), columns)
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
            walk(&fixture("join_right"), 2)[1],
            Some(NullReason::OuterJoin {
                node: "Hash Join".to_owned()
            }),
        );
    }

    #[test]
    fn malformed_plan_promotes_nothing() {
        assert_eq!(walk(&json!({}), 3), [None, None, None]);
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
            let result = walk(&json!([{ "Plan": root }]), columns);
            prop_assert_eq!(result.len(), columns);
            for promotion in result.into_iter().flatten() {
                let is_outer_join = matches!(promotion, NullReason::OuterJoin { .. });
                prop_assert!(is_outer_join);
            }
        }

        #[test]
        fn no_outer_join_no_promotion(out in prop::collection::vec("[a-c]\\.[xyz]", 0..4), columns in 0usize..6) {
            let plan = json!([{ "Plan": { "Node Type": "Seq Scan", "Output": out } }]);
            prop_assert!(walk(&plan, columns).iter().all(Option::is_none));
        }
    }
}

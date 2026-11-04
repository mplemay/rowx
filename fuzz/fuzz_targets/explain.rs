// a cargo-fuzz target: the plan walkers must tolerate any JSON, never panic
#![no_main]

use std::collections::HashSet;

use libfuzzer_sys::arbitrary::{self, Unstructured};
use libfuzzer_sys::fuzz_target;
use rowx_postgres::explain;
use serde_json::{Map, Value, json};

const NODE_TYPES: [&str; 18] = [
    "Seq Scan",
    "Index Scan",
    "Nested Loop",
    "Hash Join",
    "Append",
    "Merge Append",
    "SetOp",
    "Aggregate",
    "Sort",
    "Unique",
    "Limit",
    "Result",
    "CTE Scan",
    "Subquery Scan",
    "WorkTable Scan",
    "Recursive Union",
    "ModifyTable",
    "Gather",
];
const JOIN_TYPES: [&str; 6] = ["Inner", "Left", "Right", "Full", "Anti", "Right Anti"];
const RELATIONSHIPS: [&str; 5] = ["Outer", "Inner", "Member", "InitPlan", "SubPlan"];
const OUTPUTS: [&str; 12] = [
    "t.a",
    "u.b",
    "COALESCE(t.a, 0)",
    "count(*)",
    "NULL::integer",
    "(1)",
    "rank() OVER (ORDER BY t.a)",
    "\"T\".\"x\"",
    "old.a",
    "new.a",
    "t.a + 1",
    "",
];
const NAMES: [&str; 5] = ["t", "u", "public", "cte", "T"];

fn pick<'a>(u: &mut Unstructured<'_>, from: &[&'a str]) -> arbitrary::Result<&'a str> {
    Ok(from.get(u.choose_index(from.len())?).copied().unwrap_or_default())
}

// a plan node built from the keys the walkers read, so the fuzzer reaches their logic and not only the JSON parser
fn node(u: &mut Unstructured<'_>, depth: u8) -> arbitrary::Result<Value> {
    let mut map = Map::new();
    map.insert("Node Type".into(), json!(pick(u, &NODE_TYPES)?));
    for key in ["Join Type", "Parent Relationship"] {
        if u.arbitrary()? {
            let from: &[&str] = if key == "Join Type" { &JOIN_TYPES } else { &RELATIONSHIPS };
            map.insert(key.into(), json!(pick(u, from)?));
        }
    }
    for key in ["Schema", "Relation Name", "Alias", "CTE Name", "Subplan Name", "Operation"] {
        if u.ratio(1, 3)? {
            map.insert(key.into(), json!(pick(u, &NAMES)?));
        }
    }
    if u.arbitrary()? {
        let mut outputs = Vec::new();
        for _ in 0..u.int_in_range(0..=4)? {
            outputs.push(json!(pick(u, &OUTPUTS)?));
        }
        map.insert("Output".into(), Value::Array(outputs));
    }
    if u.ratio(1, 6)? {
        map.insert("Grouping Sets".into(), json!([{"Group Keys": [["t.a"]]}, {"Group Keys": [[]]}]));
    }
    if depth > 0 && u.arbitrary()? {
        let mut plans = Vec::new();
        for _ in 0..u.int_in_range(0..=3)? {
            plans.push(node(u, depth - 1)?);
        }
        map.insert("Plans".into(), Value::Array(plans));
    }
    Ok(Value::Object(map))
}

fn walk_all(plan: &Value, columns: usize) {
    let origins = vec![None; columns];
    let _ = explain::walk(plan, columns, &origins);
    let _ = explain::needs_function_check(plan, columns);
    let _ = explain::proven_computed(plan, columns, true);
    let _ = explain::proven_computed(plan, columns, false);
    let leaves = explain::leaf_columns(plan);
    let proven: HashSet<_> = leaves.into_iter().collect();
    let _ = explain::proven_not_null(plan, columns, &proven);
}

fn structured(u: &mut Unstructured<'_>) -> arbitrary::Result<()> {
    let columns = u.int_in_range(0..=6)?;
    let plan = json!([{ "Plan": node(u, 5)? }]);
    walk_all(&plan, columns);
    Ok(())
}

fuzz_target!(|data: &[u8]| {
    let Some((&columns, body)) = data.split_first() else {
        return;
    };
    // arbitrary JSON, including shapes no server sends
    if let Ok(plan) = serde_json::from_slice::<Value>(body) {
        walk_all(&plan, usize::from(columns % 8));
    }
    let _ = structured(&mut Unstructured::new(data));
});

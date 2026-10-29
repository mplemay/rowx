use serde_json::Value;

use super::{Marked, mark_joins, outputs};

// functions the plan prints by bare name, so a user-defined function of the same name outside `pg_catalog` would be
// indistinguishable from the built-in
pub const WHITELISTED_FUNCTIONS: [&str; 6] = [
    "count",
    "row_number",
    "rank",
    "dense_rank",
    "percent_rank",
    "cume_dist",
];

const RANKING: [&str; 5] = [
    "row_number",
    "rank",
    "dense_rank",
    "percent_rank",
    "cume_dist",
];

// the index of the bracket that closes the one at `open`, skipping quoted strings and quoted identifiers
fn matching_close(text: &str, open: usize) -> Option<usize> {
    let bytes = text.as_bytes();
    let mut depth = 0usize;
    let mut index = open;
    while let Some(&byte) = bytes.get(index) {
        match byte {
            b'\'' | b'"' => index = skip_quoted(bytes, index)?,
            b'(' => depth += 1,
            b')' => {
                depth = depth.checked_sub(1)?;
                if depth == 0 {
                    return Some(index);
                }
            }
            _ => {}
        }
        index += 1;
    }
    None
}

// the index of the quote that ends the quoted run starting at `start`; a doubled quote is an escaped one
fn skip_quoted(bytes: &[u8], start: usize) -> Option<usize> {
    let quote = *bytes.get(start)?;
    let mut index = start + 1;
    while let Some(&byte) = bytes.get(index) {
        if byte == quote {
            if bytes.get(index + 1) == Some(&quote) {
                index += 1;
            } else {
                return Some(index);
            }
        }
        index += 1;
    }
    None
}

// removes outer parentheses only while the first one closes at the very end
fn strip_parens(text: &str) -> &str {
    let mut text = text.trim();
    while text.starts_with('(')
        && matching_close(text, 0).is_some_and(|close| close + 1 == text.len())
    {
        text = text.get(1..text.len() - 1).unwrap_or_default().trim();
    }
    text
}

// splits at commas that are outside brackets and quotes
fn split_args(text: &str) -> Vec<&str> {
    let bytes = text.as_bytes();
    let mut parts = Vec::new();
    let mut depth = 0usize;
    let mut start = 0;
    let mut index = 0;
    while let Some(&byte) = bytes.get(index) {
        match byte {
            b'\'' | b'"' => {
                let Some(end) = skip_quoted(bytes, index) else {
                    return vec![text];
                };
                index = end;
            }
            b'(' | b'[' => depth += 1,
            b')' | b']' => depth = depth.saturating_sub(1),
            b',' if depth == 0 => {
                parts.push(text.get(start..index).unwrap_or_default());
                start = index + 1;
            }
            _ => {}
        }
        index += 1;
    }
    parts.push(text.get(start..).unwrap_or_default());
    parts
}

fn is_number(text: &str) -> bool {
    let digits = |s: &str| -> usize { s.bytes().take_while(u8::is_ascii_digit).count() };
    let text = text.strip_prefix('-').unwrap_or(text);
    let mut rest = text;
    let whole = digits(rest);
    if whole == 0 {
        return false;
    }
    rest = rest.get(whole..).unwrap_or_default();
    if let Some(fraction) = rest.strip_prefix('.') {
        rest = fraction.get(digits(fraction)..).unwrap_or_default();
    }
    if let Some(exponent) = rest.strip_prefix(['e', 'E']) {
        let exponent = exponent.strip_prefix(['+', '-']).unwrap_or(exponent);
        let count = digits(exponent);
        return count > 0 && exponent.len() == count;
    }
    rest.is_empty()
}

// `'text'`, optionally cast to a plain type name; `NULL::t` is neither, so it never matches
fn is_string_constant(text: &str) -> bool {
    if !text.starts_with('\'') {
        return false;
    }
    let Some(end) = skip_quoted(text.as_bytes(), 0) else {
        return false;
    };
    let rest = text.get(end + 1..).unwrap_or_default();
    rest.is_empty()
        || rest.strip_prefix("::").is_some_and(|ty| {
            !ty.is_empty()
                && ty
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | ' ' | '[' | ']'))
        })
}

fn is_constant(text: &str) -> bool {
    is_number(text) || is_string_constant(text) || matches!(text, "true" | "false")
}

// `OVER (...)` or `OVER name`, with nothing after it
fn is_over_clause(rest: &str) -> bool {
    let Some(window) = rest.strip_prefix("OVER") else {
        return false;
    };
    if !window.starts_with([' ', '(']) {
        return false;
    }
    let window = window.trim();
    if window.starts_with('(') {
        return matching_close(window, 0).is_some_and(|close| close + 1 == window.len());
    }
    !window.is_empty()
        && window
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_')
}

// Whether the printed expression is never NULL. `functions` is false when a user-defined function may shadow a
// whitelisted name.
fn is_not_null(expression: &str, functions: bool) -> bool {
    let text = strip_parens(expression);
    if is_constant(text) {
        return true;
    }
    let name_end = text
        .find(|c: char| !(c.is_ascii_alphanumeric() || c == '_'))
        .unwrap_or(text.len());
    let (name, after) = text.split_at(name_end);
    if name.is_empty() || !after.starts_with('(') {
        return false;
    }
    let Some(close) = matching_close(after, 0) else {
        return false;
    };
    let args = after.get(1..close).unwrap_or_default();
    let rest = after.get(close + 1..).unwrap_or_default().trim();
    match name {
        "COALESCE" if rest.is_empty() => split_args(args)
            .into_iter()
            .any(|arg| is_not_null(arg, functions)),
        // a FILTER clause is the rest, which neither branch accepts
        "count" if functions => rest.is_empty() || is_over_clause(rest),
        _ if functions && RANKING.contains(&name) => args.trim().is_empty() && is_over_clause(rest),
        // the built-in is deparsed as special syntax in capitals, and `merge_action()` always parses as that syntax, so a user-defined function cannot take its place
        "MERGE_ACTION" => args.trim().is_empty() && rest.is_empty(),
        _ => false,
    }
}

fn root_outputs(plan: &Value) -> Option<(&Value, Vec<&str>)> {
    let root = plan.get(0)?.get("Plan")?;
    Some((root, outputs(root).collect()))
}

/// Whether any top-level column is only proven because of a whitelisted function name, so the caller must check
/// that no user-defined function shadows it.
#[must_use]
pub fn needs_function_check(plan: &Value, column_count: usize) -> bool {
    root_outputs(plan).is_some_and(|(_, columns)| {
        columns
            .iter()
            .take(column_count)
            .any(|expr| is_not_null(expr, true) && !is_not_null(expr, false))
    })
}

/// Per top-level column: whether the root node's output expression is never NULL. An expression that an outer
/// join null-extends is not trusted.
#[must_use]
pub fn proven_computed(plan: &Value, column_count: usize, functions: bool) -> Vec<bool> {
    let Some((root, columns)) = root_outputs(plan) else {
        return vec![false; column_count];
    };
    let mut marked = Marked::new();
    mark_joins(root, None, &mut marked);
    (0..column_count)
        .map(|position| {
            columns.get(position).is_some_and(|expr| {
                !marked.contains_key(*expr)
                    && !marked.contains_key(strip_parens(expr))
                    && is_not_null(expr, functions)
            })
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::{is_not_null, needs_function_check, proven_computed};

    #[test]
    fn constants_are_not_null() {
        for expr in [
            "1",
            "(1)",
            "-1",
            "1.5",
            "1e10",
            "2.5E-3",
            "'x'::text",
            "'it''s (FILTER)'::text",
            "'x'::character varying",
            "true",
            "(false)",
        ] {
            assert!(is_not_null(expr, false), "{expr}");
        }
    }

    #[test]
    fn null_and_computed_are_nullable() {
        for expr in [
            "NULL::integer",
            "NULL::text",
            "NULL",
            "upper(u.name)",
            "(u.id + 1)",
            "1 + 1",
            "1e",
            "'x",
            "'x'::",
            "$1",
            "",
            "u.id",
        ] {
            assert!(!is_not_null(expr, true), "{expr}");
        }
    }

    #[test]
    fn count_is_not_null_unless_filtered() {
        assert!(is_not_null("count(*)", true));
        assert!(is_not_null("(count(DISTINCT u.id))", true));
        assert!(is_not_null("count(*) OVER (?)", true));
        assert!(is_not_null("(count(*) OVER w)", true));
        assert!(!is_not_null("count(*) FILTER (WHERE (u.id > 1))", true));
        assert!(!is_not_null(
            "count(*) FILTER (WHERE (u.id > 1)) OVER (?)",
            true
        ));
        assert!(!is_not_null("count(*)", false));
        assert!(!is_not_null("myschema.count(*)", true));
        assert!(!is_not_null("count(*) OVER (?) + 1", true));
        assert!(!is_not_null("count(*) OVER (", true));
    }

    #[test]
    fn ranking_functions_need_over() {
        for name in [
            "row_number",
            "rank",
            "dense_rank",
            "percent_rank",
            "cume_dist",
        ] {
            assert!(is_not_null(&format!("({name}() OVER (?))"), true), "{name}");
            assert!(!is_not_null(&format!("{name}()"), true), "{name}");
            assert!(!is_not_null(&format!("{name}() OVER (?)"), false), "{name}");
        }
        // a hypothetical-set aggregate is not the window function
        assert!(!is_not_null("rank(1) WITHIN GROUP (ORDER BY u.id)", true));
        assert!(!is_not_null("rank(1) OVER (?)", true));
    }

    #[test]
    fn merge_action_is_not_null() {
        assert!(is_not_null("MERGE_ACTION()", true));
        assert!(is_not_null("MERGE_ACTION()", false));
        assert!(!is_not_null("MERGE_ACTION(1)", true));
        // a user-defined function of that name is printed as an ordinary call
        assert!(!is_not_null("merge_action()", true));
    }

    #[test]
    fn coalesce_needs_one_proven_argument() {
        assert!(is_not_null("COALESCE(u.email, 'x'::text)", false));
        assert!(is_not_null("COALESCE(u.email, COALESCE(u.name, 1))", false));
        assert!(is_not_null("COALESCE(upper(u.a, (1, 2)), 3)", false));
        assert!(is_not_null("COALESCE(u.email, count(*))", true));
        assert!(!is_not_null("COALESCE(u.email, count(*))", false));
        assert!(!is_not_null("COALESCE(u.email, u.name)", true));
        assert!(!is_not_null("COALESCE(NULL::text, u.name)", true));
        // a comma inside a string or a nested call is not an argument boundary
        assert!(!is_not_null("COALESCE(u.email, upper('a, 1'))", true));
        assert!(!is_not_null("COALESCE(u.email, f(1, 2))", true));
    }

    #[test]
    fn function_check_is_needed_only_for_function_proofs() {
        let plan =
            |output: &[&str]| json!([{ "Plan": { "Node Type": "Aggregate", "Output": output } }]);
        assert!(needs_function_check(&plan(&["(count(*))"]), 1));
        assert!(!needs_function_check(&plan(&["1"]), 1));
        assert!(!needs_function_check(&plan(&["COALESCE(u.a, 1)"]), 1));
        assert!(!needs_function_check(&plan(&["u.a"]), 1));
    }

    #[test]
    fn null_extended_outputs_are_not_trusted() {
        let plan = json!([{ "Plan": {
            "Node Type": "Nested Loop", "Join Type": "Left", "Output": ["a.x", "(1)"],
            "Plans": [
                { "Node Type": "Seq Scan", "Parent Relationship": "Outer", "Output": ["a.x"] },
                { "Node Type": "Result", "Parent Relationship": "Inner", "Output": ["(1)"] },
            ],
        } }]);
        assert_eq!(proven_computed(&plan, 2, true), vec![false, false]);
        let flat = json!([{ "Plan": { "Node Type": "Result", "Output": ["1", "(count(*))"] } }]);
        assert_eq!(proven_computed(&flat, 2, true), vec![true, true]);
        assert_eq!(proven_computed(&flat, 2, false), vec![true, false]);
    }
}

// Guards the rule that SQL rowx sends on its own names `pg_catalog` objects explicitly, because functions,
// operators, types and catalog tables all resolve through the session's `search_path`.
//
// What it covers: every string literal in `crates/*/src/**/*.rs` (outside `#[cfg(test)]` modules) whose first
// word is an SQL verb (case-insensitive). In each one it flags, outside string literals and comments:
//   - a function call `name(` that is not `pg_catalog.name(`, other than grammar keywords such as `EXISTS`
//     or `COALESCE`, an alias column list after `AS`, and a column list after a quoted or placeholder name;
//   - an operator that is not written `OPERATOR(pg_catalog.<op>)`, except the `*` wildcard and the `=` of a
//     `SET` assignment;
//   - `IN`, `BETWEEN`, `LIKE`, `ILIKE` and `SIMILAR`, which look an operator up by name;
//   - a `pg_*` name that is not `pg_catalog.pg_*`, and a cast to a type that is not a grammar keyword
//     (`bigint`, `boolean`, ...) and not `pg_catalog.<type>`;
//   - forms that look an operator up by name without writing it: `IS [NOT] DISTINCT FROM`, `NULLIF`, a simple
//     `CASE x WHEN`, `CAST(x AS type)` and a typed literal such as `oid '1'`;
//   - in a `CREATE` statement, a well-known catalog type name (`text`, `oid`, `int8`, ...) used as a column type.
// What it does not cover:
//   - a literal whose first word (after leading whitespace) is not one of the SQL verbs listed below;
//   - later pieces of `concat!`, `+`, `push_str` or `write!` that do not start with a verb;
//   - names behind `format!` placeholders (`{t}` is treated as a quoted name, not checked);
//   - `\u{..}` and `\x..` escapes, which are not decoded;
//   - SQL built at runtime from user text (a migration file, a user query), and a type name outside the lists below;
//   - SQL in Python (`src/rowx/testing.py` and the rest of `src/rowx`), which is audited by hand.
//
// A name that is genuinely fine does not get an allowlist entry here: qualify it instead.

use std::fs;
use std::path::{Path, PathBuf};

type TestResult<T = ()> = Result<T, Box<dyn std::error::Error>>;

const VERBS: &[&str] = &[
    "select",
    "insert",
    "update",
    "delete",
    "create",
    "with",
    "reset",
    "close",
    "unlisten",
    "discard",
    "set",
    "begin",
    "start",
    "commit",
    "rollback",
    "savepoint",
    "release",
    "explain",
    "drop",
    "alter",
    "comment",
    "listen",
    "notify",
    "lock",
    "truncate",
    "prepare",
    "deallocate",
];

// keywords that may be followed by `(` without being a function call
const PAREN_KEYWORDS: &[&str] = &[
    "all",
    "and",
    "any",
    "array",
    "as",
    "coalesce",
    "exists",
    "extract",
    "from",
    "join",
    "key",
    "not",
    "on",
    "or",
    "select",
    "then",
    "values",
    "when",
    "where",
    "else",
    "case",
    "by",
    "limit",
    "unique",
    "check",
    "references",
    "default",
    "primary",
    "with",
    "explain",
];

// types the grammar maps to `pg_catalog` itself, so a bare cast to one cannot be shadowed
const GRAMMAR_TYPES: &[&str] = &[
    "bigint",
    "int",
    "integer",
    "smallint",
    "boolean",
    "real",
    "numeric",
    "decimal",
    "bit",
    "varchar",
    "char",
    "character",
    "timestamp",
    "time",
    "interval",
    "float",
];

// catalog types that are ordinary names to the grammar
const CATALOG_TYPES: &[&str] = &[
    "text",
    "oid",
    "int2",
    "int4",
    "int8",
    "float4",
    "float8",
    "bytea",
    "timestamptz",
    "regclass",
    "regtype",
    "name",
    "uuid",
    "json",
    "jsonb",
    "bpchar",
    "date",
    "xid",
    "tid",
];

const NO_OPERATOR_KEYWORDS: &[&str] = &["in", "between", "like", "ilike", "similar"];

const OPERATOR_CHARS: &str = "+-*/<>=~!@#%^&|?";

#[derive(Debug, Clone, PartialEq, Eq)]
enum Token {
    Ident(String),
    Quoted,
    Str,
    Placeholder,
    Op(String),
    Open,
    Close,
    Comma,
    Semi,
    Dot,
    Cast,
    Other,
}

fn tokenize(sql: &str) -> Vec<Token> {
    let chars: Vec<char> = sql.chars().collect();
    let mut tokens = Vec::new();
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        let next = chars.get(i + 1).copied();
        if c.is_whitespace() {
            i += 1;
        } else if c == '-' && next == Some('-') {
            while i < chars.len() && chars[i] != '\n' && chars[i] != '\r' {
                i += 1;
            }
        } else if c == '/' && next == Some('*') {
            i = skip_block(&chars, i + 2);
        } else if c == '\'' || c == '"' {
            i = skip_quoted(&chars, i, c);
            tokens.push(if c == '"' { Token::Quoted } else { Token::Str });
        } else if c == '{' {
            while i < chars.len() && chars[i] != '}' {
                i += 1;
            }
            i += 1;
            tokens.push(Token::Placeholder);
        } else if c == '$' && next.is_some_and(|n| n.is_ascii_digit()) {
            i += 1;
            while chars.get(i).is_some_and(char::is_ascii_digit) {
                i += 1;
            }
            tokens.push(Token::Other);
        } else if c.is_ascii_alphabetic() || c == '_' {
            let start = i;
            while chars
                .get(i)
                .is_some_and(|ch| ch.is_ascii_alphanumeric() || *ch == '_' || *ch == '$')
            {
                i += 1;
            }
            let word: String = chars[start..i].iter().collect();
            tokens.push(Token::Ident(word.to_ascii_lowercase()));
        } else if c.is_ascii_digit() {
            while chars
                .get(i)
                .is_some_and(|ch| ch.is_ascii_alphanumeric() || *ch == '.')
            {
                i += 1;
            }
            tokens.push(Token::Other);
        } else if c == ':' && next == Some(':') {
            i += 2;
            tokens.push(Token::Cast);
        } else if OPERATOR_CHARS.contains(c) {
            let start = i;
            while chars.get(i).is_some_and(|ch| OPERATOR_CHARS.contains(*ch)) {
                i += 1;
            }
            tokens.push(Token::Op(chars[start..i].iter().collect()));
        } else {
            i += 1;
            tokens.push(match c {
                '(' => Token::Open,
                ')' => Token::Close,
                ',' => Token::Comma,
                ';' => Token::Semi,
                '.' => Token::Dot,
                _ => Token::Other,
            });
        }
    }
    tokens
}

// `from` is just past the opening `/*`; block comments nest
fn skip_block(chars: &[char], from: usize) -> usize {
    let mut depth = 1_usize;
    let mut i = from;
    while i < chars.len() && depth > 0 {
        match (chars[i], chars.get(i + 1)) {
            ('/', Some('*')) => {
                depth += 1;
                i += 2;
            }
            ('*', Some('/')) => {
                depth -= 1;
                i += 2;
            }
            _ => i += 1,
        }
    }
    i
}

// a quote doubles to escape itself; returns the index just past the closing quote
fn skip_quoted(chars: &[char], open: usize, quote: char) -> usize {
    let mut i = open + 1;
    while i < chars.len() {
        if chars[i] == quote {
            if chars.get(i + 1) == Some(&quote) {
                i += 2;
                continue;
            }
            return i + 1;
        }
        i += 1;
    }
    i
}

fn is_ident(token: Option<&Token>, name: &str) -> bool {
    matches!(token, Some(Token::Ident(word)) if word == name)
}

// whether the tokens before `at` end in `pg_catalog .`
fn qualified(tokens: &[Token], at: usize) -> bool {
    at >= 2 && tokens[at - 1] == Token::Dot && is_ident(tokens.get(at - 2), "pg_catalog")
}

fn violations(sql: &str) -> Vec<String> {
    let tokens = tokenize(sql);
    let mut found = Vec::new();
    let mut in_set = false;
    let mut depth = 0_usize;
    // the paren depth to return to when the current `OPERATOR(...)` closes
    let mut operator_depth: Option<usize> = None;
    for (at, token) in tokens.iter().enumerate() {
        let prev = at.checked_sub(1).and_then(|p| tokens.get(p));
        let next = tokens.get(at + 1);
        match token {
            Token::Open => depth += 1,
            Token::Close => {
                depth = depth.saturating_sub(1);
                if operator_depth == Some(depth) {
                    operator_depth = None;
                }
            }
            Token::Semi => in_set = false,
            Token::Ident(word) => {
                if word == "set" {
                    in_set = true;
                }
                if matches!(word.as_str(), "where" | "from" | "returning") {
                    in_set = false;
                }
                if word == "operator" && next == Some(&Token::Open) {
                    operator_depth = Some(depth);
                    let qualified_op = is_ident(tokens.get(at + 2), "pg_catalog")
                        && tokens.get(at + 3) == Some(&Token::Dot);
                    if !qualified_op {
                        found.push("OPERATOR(...) without a pg_catalog qualifier".to_owned());
                    }
                    continue;
                }
                check_ident(&tokens, at, word, &mut found);
                check_name_resolving_forms(&tokens, at, word, &mut found);
            }
            Token::Op(op) => {
                if operator_depth.is_some() {
                    continue;
                }
                let wildcard = op == "*"
                    && (matches!(prev, Some(Token::Open | Token::Comma | Token::Dot))
                        || is_ident(prev, "select")
                        || is_ident(prev, "unlisten"));
                let assignment = op == "=" && in_set && depth == 0;
                if !wildcard && !assignment {
                    found.push(format!("bare operator `{op}`"));
                }
            }
            _ => {}
        }
    }
    found
}

fn check_ident(tokens: &[Token], at: usize, word: &str, found: &mut Vec<String>) {
    let prev = at.checked_sub(1).and_then(|p| tokens.get(p));
    let after_dot = prev == Some(&Token::Dot);
    if tokens.get(at + 1) == Some(&Token::Open)
        && !PAREN_KEYWORDS.contains(&word)
        && !qualified(tokens, at)
        && !is_ident(prev, "as")
        && !matches!(prev, Some(Token::Quoted | Token::Placeholder))
    {
        found.push(format!("unqualified function call `{word}(`"));
    }
    if NO_OPERATOR_KEYWORDS.contains(&word) && !after_dot {
        found.push(format!("`{word}` looks an operator up by name"));
    }
    if word.starts_with("pg_") && word != "pg_catalog" && !qualified(tokens, at) {
        found.push(format!("unqualified catalog name `{word}`"));
    }
    if prev == Some(&Token::Cast) && !GRAMMAR_TYPES.contains(&word) && word != "pg_catalog" {
        found.push(format!("cast to unqualified type `{word}`"));
    }
    let creates = is_ident(tokens.first(), "create");
    let column_type = matches!(
        prev,
        Some(Token::Ident(_) | Token::Quoted | Token::Placeholder)
    );
    if creates && column_type && CATALOG_TYPES.contains(&word) {
        found.push(format!("unqualified type name `{word}`"));
    }
}

// forms whose operator or type is resolved by name even though no operator is written
fn check_name_resolving_forms(tokens: &[Token], at: usize, word: &str, found: &mut Vec<String>) {
    let prev = at.checked_sub(1).and_then(|p| tokens.get(p));
    let next = tokens.get(at + 1);
    if word == "distinct" && (is_ident(prev, "is") || is_ident(prev, "not")) {
        found.push("`IS [NOT] DISTINCT FROM` looks an operator up by name".to_owned());
    }
    if word == "nullif" {
        found.push("`NULLIF` looks an operator up by name".to_owned());
    }
    if word == "case" && !is_ident(next, "when") {
        found.push("a simple `CASE x WHEN` looks an operator up by name".to_owned());
    }
    if word == "cast" && next == Some(&Token::Open) {
        found.push("`CAST(x AS type)` resolves the type by name".to_owned());
    }
    if CATALOG_TYPES.contains(&word) && next == Some(&Token::Str) && prev != Some(&Token::Dot) {
        found.push(format!("typed literal of unqualified type `{word}`"));
    }
}

// the index past a char literal that starts at `at`; a lifetime has no closing quote, so only the quote is skipped
fn skip_char_literal(chars: &[char], at: usize) -> usize {
    if chars.get(at + 1) == Some(&'\\') {
        // `'\n'`, `'\''`, `'\x41'` and `'\u{1F600}'` all end at the next quote after the escaped character
        let mut i = at + 3;
        while i < chars.len() && chars[i] != '\'' && i < at + 12 {
            i += 1;
        }
        i + 1
    } else if chars.get(at + 2) == Some(&'\'') {
        at + 3
    } else {
        at + 1
    }
}

// a raw string `r"..."`, `r#"..."#`, `br##"..."##` that starts at `at`: its body and the index past its closing quote
fn read_raw_string(chars: &[char], at: usize) -> Option<(String, usize)> {
    let ident = |c: char| c.is_ascii_alphanumeric() || c == '_';
    let before = at.checked_sub(1).map(|p| chars[p]);
    let prefixed = before == Some('b') && at.checked_sub(2).is_none_or(|p| !ident(chars[p]));
    if chars.get(at) != Some(&'r') || before.is_some_and(ident) && !prefixed {
        return None;
    }
    let mut i = at + 1;
    let mut hashes = 0_usize;
    while chars.get(i) == Some(&'#') {
        hashes += 1;
        i += 1;
    }
    if chars.get(i) != Some(&'"') {
        return None;
    }
    i += 1;
    let start = i;
    while i < chars.len() {
        if chars[i] == '"' && (1..=hashes).all(|n| chars.get(i + n) == Some(&'#')) {
            return Some((chars[start..i].iter().collect(), i + 1 + hashes));
        }
        i += 1;
    }
    Some((chars[start..].iter().collect(), chars.len()))
}

// the index past the line comment, block comment, string or char literal at `at`, if there is one
fn skip_non_code(chars: &[char], at: usize) -> Option<usize> {
    match (chars[at], chars.get(at + 1)) {
        ('/', Some('/')) => Some(
            chars[at..]
                .iter()
                .position(|c| *c == '\n')
                .map_or(chars.len(), |n| at + n),
        ),
        ('/', Some('*')) => Some(skip_block(chars, at + 2)),
        ('"', _) => Some(read_string(chars, at + 1).1),
        ('\'', _) => Some(skip_char_literal(chars, at)),
        ('r' | 'b', _) => read_raw_string(chars, at).map(|(_, end)| end),
        _ => None,
    }
}

// the source without its `#[cfg(test)] mod name { ... }` blocks, which other items may follow. A body-less
// `#[cfg(test)] mod name;` drops only that item. If the scanner loses its place the failure is in the safe direction
// for a lint: it drops less or more of the file, and the extractor test notices statements that go missing.
fn without_test_modules(source: &str) -> String {
    const MARKER: &str = "#[cfg(test)]";
    let mut out = String::new();
    let mut rest = source;
    while let Some(at) = rest.find(MARKER) {
        out.push_str(&rest[..at]);
        let after = &rest[at + MARKER.len()..];
        let Some(declared) = after.trim_start().strip_prefix("mod ") else {
            // a cfg(test) item that is not a module stays in the scan
            out.push_str(MARKER);
            rest = after;
            continue;
        };
        // `mod name;` has no body; `mod name {` does
        let Some(end) = declared.find(['{', ';']) else {
            return out;
        };
        let body_start = &declared[end..];
        if let Some(after_semi) = body_start.strip_prefix(';') {
            rest = after_semi;
            continue;
        }
        let chars: Vec<char> = body_start.chars().collect();
        let mut depth = 0_usize;
        let mut i = 0;
        while i < chars.len() {
            if let Some(next) = skip_non_code(&chars, i) {
                i = next;
                continue;
            }
            match chars[i] {
                '{' => depth += 1,
                '}' => {
                    depth = depth.saturating_sub(1);
                    if depth == 0 {
                        i += 1;
                        break;
                    }
                }
                _ => {}
            }
            i += 1;
        }
        let consumed: usize = chars[..i.min(chars.len())]
            .iter()
            .map(|c| c.len_utf8())
            .sum();
        rest = &body_start[consumed..];
    }
    out.push_str(rest);
    out
}

// the string literals of a Rust file that look like SQL, outside its test modules and comments
fn sql_literals(source: &str) -> Vec<String> {
    let source = without_test_modules(source);
    let chars: Vec<char> = source.chars().collect();
    let mut literals = Vec::new();
    let mut i = 0;
    while i < chars.len() {
        let literal = if let Some((text, end)) = read_raw_string(&chars, i) {
            i = end;
            Some(text)
        } else if chars[i] == '"' {
            let (text, end) = read_string(&chars, i + 1);
            i = end;
            Some(text)
        } else {
            i = skip_non_code(&chars, i).unwrap_or(i + 1);
            None
        };
        if let Some(text) = literal {
            let first = text
                .split_whitespace()
                .next()
                .unwrap_or_default()
                .to_ascii_lowercase();
            if VERBS.contains(&first.as_str()) {
                literals.push(text);
            }
        }
    }
    literals
}

// the unescaped body of a normal string literal that starts at `from`, and the index past its closing quote
fn read_string(chars: &[char], from: usize) -> (String, usize) {
    let mut text = String::new();
    let mut i = from;
    while i < chars.len() {
        match chars[i] {
            '"' => return (text, i + 1),
            '\\' => {
                i += 1;
                match chars.get(i) {
                    Some('n') => text.push('\n'),
                    Some('r') => text.push('\r'),
                    Some('t') => text.push('\t'),
                    Some('\n') => {
                        // a line continuation drops the newline and the next line's indentation
                        while chars.get(i + 1).is_some_and(|c| c.is_whitespace()) {
                            i += 1;
                        }
                    }
                    Some(other) => text.push(*other),
                    None => {}
                }
                i += 1;
            }
            c => {
                text.push(c);
                i += 1;
            }
        }
    }
    (text, i)
}

fn rust_files(dir: &Path, out: &mut Vec<PathBuf>) -> TestResult {
    for entry in fs::read_dir(dir)? {
        let path = entry?.path();
        if path.is_dir() {
            rust_files(&path, out)?;
        } else if path.extension().is_some_and(|ext| ext == "rs") {
            out.push(path);
        }
    }
    Ok(())
}

fn crate_sources() -> TestResult<Vec<PathBuf>> {
    let crates = Path::new(env!("CARGO_MANIFEST_DIR")).join("..");
    let mut files = Vec::new();
    for entry in fs::read_dir(&crates)? {
        let src = entry?.path().join("src");
        if src.is_dir() {
            rust_files(&src, &mut files)?;
        }
    }
    files.sort();
    Ok(files)
}

#[test]
fn the_scanner_flags_what_it_claims_to() {
    for bad in [
        "SELECT now()",
        "SELECT 1 FROM t WHERE a = b",
        "SELECT a::text FROM t",
        "SELECT 1 FROM pg_class",
        "SELECT 1 WHERE a IN (1)",
        "SELECT 1 FROM t WHERE a OPERATOR(=) b",
        "CREATE TABLE {t} (a text)",
        "SELECT 1 FROM evil.f(1)",
        "SELECT a + 1",
        "SELECT 1 WHERE a IS DISTINCT FROM b",
        "SELECT 1 WHERE a IS NOT DISTINCT FROM b",
        "SELECT NULLIF(a, b)",
        "SELECT CASE a WHEN 1 THEN 2 END",
        "SELECT CAST(a AS text)",
        "SELECT oid '1'",
    ] {
        assert!(!violations(bad).is_empty(), "{bad}");
    }
    for good in [
        "SELECT pg_catalog.now()",
        "SELECT 1 FROM t WHERE a OPERATOR(pg_catalog.=) b",
        "SELECT a::pg_catalog.text, b::bigint FROM pg_catalog.pg_class AS c WHERE EXISTS (SELECT 1)",
        "SELECT 'now() = x', \"a\" -- b = c\nFROM t",
        "UPDATE {t} SET a = $1, b = $2 WHERE c OPERATOR(pg_catalog.=) $3",
        "SELECT * FROM pg_catalog.unnest($1::pg_catalog.oid[]) AS o(a, b)",
        "INSERT INTO {t} (a, b) VALUES ($1, $2)",
        "UNLISTEN *;",
        "SELECT DISTINCT a, CASE WHEN b THEN 1 ELSE 2 END FROM t",
        "SELECT pg_catalog.oid '1', interval '1 day'",
    ] {
        assert_eq!(violations(good), Vec::<String>::new(), "{good}");
    }
}

#[test]
fn a_bodyless_test_module_does_not_swallow_the_next_block() {
    let kept = without_test_modules("#[cfg(test)] mod t;\nfn f() { \"SELECT now()\" }");
    assert!(sql_literals(&kept).iter().any(|sql| sql == "SELECT now()"));
    let dropped = without_test_modules(
        "#[cfg(test)] mod t { fn a() { let c = '}'; let d = '\\x41'; /* } */ let s = r#\"}\"#; } }\nfn f() { \"SELECT now()\" }",
    );
    assert!(
        sql_literals(&dropped)
            .iter()
            .any(|sql| sql == "SELECT now()")
    );
    assert!(!dropped.contains("let c"));
}

#[test]
fn raw_strings_end_at_their_own_hashes() {
    let found = sql_literals("fn f() { r#\"SELECT \"a\", now()\"#; r\"SELECT 1\"; }");
    assert_eq!(found, ["SELECT \"a\", now()", "SELECT 1"]);
    assert_eq!(violations(&found[0]), ["unqualified function call `now(`"]);
}

#[test]
fn the_extractor_finds_the_known_statements() -> TestResult {
    let mut all = Vec::new();
    for file in crate_sources()? {
        all.extend(sql_literals(&fs::read_to_string(file)?));
    }
    for known in [
        "RESET SESSION AUTHORIZATION",
        "CREATE TABLE IF NOT EXISTS",
        "SELECT pg_catalog.pg_advisory_lock",
        "SELECT o.relid",
        "SELECT l.s",
        "SELECT 1 OPERATOR(pg_catalog./)",
        "SELECT d.datname",
    ] {
        assert!(
            all.iter().any(|sql| sql.starts_with(known)),
            "no literal starts with {known}"
        );
    }
    Ok(())
}

#[test]
fn internal_sql_names_pg_catalog_objects_explicitly() -> TestResult {
    let mut failures = Vec::new();
    for file in crate_sources()? {
        for sql in sql_literals(&fs::read_to_string(&file)?) {
            for violation in violations(&sql) {
                failures.push(format!(
                    "{}: {violation} in `{}`",
                    file.display(),
                    sql.trim()
                ));
            }
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
    Ok(())
}

use std::collections::BTreeSet;

use rusqlite::Connection;

use super::{Class, MODELED_OPCODES, Op, REVIEWED_SQLITE_VERSION, Silent, analyze, read_program};
use crate::describe::{Construct, Structure, describe};
use crate::types::Kind;

const SCHEMA: &str = "
    CREATE TABLE t (id INTEGER PRIMARY KEY, a INTEGER NOT NULL, b TEXT, c,
        g INTEGER GENERATED ALWAYS AS (a + 1) VIRTUAL);
    CREATE TABLE s (id INTEGER PRIMARY KEY, x TEXT NOT NULL, y TEXT, r REAL NOT NULL, n INTEGER) STRICT;
    CREATE TABLE w (k INTEGER PRIMARY KEY, v TEXT) WITHOUT ROWID;
    CREATE TABLE log (m TEXT);
    CREATE TABLE big (a INTEGER NOT NULL, b INTEGER, c TEXT);
    CREATE INDEX big_ab ON big (a, b);
    CREATE INDEX big_c ON big (c);
    CREATE VIEW v AS SELECT a, b FROM t;
    CREATE INDEX t_b ON t (b);
    CREATE INDEX s_y ON s (y, x);
    CREATE TRIGGER t_ai AFTER INSERT ON t BEGIN INSERT INTO log (m) VALUES ('x'); END;
";

type TestResult = Result<(), Box<dyn std::error::Error>>;

fn database() -> rusqlite::Result<Connection> {
    let conn = Connection::open_in_memory()?;
    conn.execute_batch(SCHEMA)?;
    Ok(conn)
}

// (class, never NULL, a plain read of a base column)
type Col = (Option<Class>, bool, bool);

const I: Option<Class> = Some(Class::Integer);
const R: Option<Class> = Some(Class::Real);
const T: Option<Class> = Some(Class::Text);
const B: Option<Class> = Some(Class::Blob);
const U: Option<Class> = None;

enum Expect {
    Cols(&'static [Col]),
    // the analysis reports nothing
    Silent,
}

use Expect::{Cols, Silent as Quiet};

struct Case {
    name: &'static str,
    sql: &'static str,
    expect: Expect,
}

const fn case(name: &'static str, sql: &'static str, expect: Expect) -> Case {
    Case { name, sql, expect }
}

const CASES: &[Case] = &[
    // literals
    case("integer literal", "SELECT 1", Cols(&[(I, true, false)])),
    case(
        "text, real, blob and null literals",
        "SELECT 'x', 1.5, x'00', NULL",
        Cols(&[
            (T, true, false),
            (R, true, false),
            (B, true, false),
            (U, false, false),
        ]),
    ),
    // aggregates
    case(
        "count star",
        "SELECT count(*) FROM t",
        Cols(&[(I, true, false)]),
    ),
    case(
        "count and total over an empty table",
        "SELECT count(b), total(a) FROM t",
        Cols(&[(I, true, false), (R, true, false)]),
    ),
    case(
        "sum, max, min are nullable and untyped, avg is a nullable real",
        "SELECT sum(a), max(a), min(a), avg(a) FROM t",
        Cols(&[
            (U, false, false),
            (U, false, false),
            (U, false, false),
            (R, false, false),
        ]),
    ),
    case(
        "strict aggregates",
        "SELECT total(r), sum(r), avg(n), count(n) FROM s",
        Cols(&[
            (R, true, false),
            (U, false, false),
            (R, false, false),
            (I, true, false),
        ]),
    ),
    case(
        "count beside a bare column without group by",
        "SELECT count(*), max(a) FROM t WHERE b LIKE 'x%'",
        Cols(&[(I, true, false), (U, false, false)]),
    ),
    case(
        "group_concat is nullable text",
        "SELECT group_concat(b), group_concat(x, ',') FROM t, s",
        Cols(&[(T, false, false), (T, false, false)]),
    ),
    case(
        "aggregate with group by",
        "SELECT a, count(*) FROM t GROUP BY a",
        // the key is read back through the sorter, which the analysis leaves to the declared rule
        Cols(&[(U, false, false), (I, true, false)]),
    ),
    case(
        "aggregates with group by and a nullable key",
        "SELECT b, count(*), sum(a) FROM t GROUP BY b",
        Cols(&[(U, false, false), (I, true, false), (U, false, false)]),
    ),
    case(
        "group by having",
        "SELECT min(a), count(*) FROM t GROUP BY b HAVING count(*) > 1",
        Cols(&[(U, false, false), (I, true, false)]),
    ),
    // coalesce and case
    case(
        "coalesce of a nullable untyped column and a literal",
        "SELECT coalesce(b, 0) FROM t",
        Cols(&[(U, true, false)]),
    ),
    case(
        "coalesce and ifnull of a strict nullable text column",
        "SELECT coalesce(y, 'z'), ifnull(y, 'q'), coalesce(x, 'z') FROM s",
        Cols(&[(T, true, false), (T, true, false), (T, true, true)]),
    ),
    case(
        "ifnull chains in a concatenation",
        "SELECT ifnull(b, 'x') || ifnull(c, 'y') FROM t",
        Cols(&[(T, true, false)]),
    ),
    case(
        "case with every arm non-null",
        "SELECT CASE WHEN a > 1 THEN 'x' ELSE 'y' END FROM t",
        Cols(&[(T, true, false)]),
    ),
    case(
        "case without else is nullable",
        "SELECT CASE WHEN a > 1 THEN 'x' END FROM t",
        Cols(&[(T, false, false)]),
    ),
    case(
        "case with a nullable arm",
        "SELECT CASE WHEN a > 1 THEN b ELSE 'z' END FROM t",
        Cols(&[(U, false, false)]),
    ),
    case(
        "simple case",
        "SELECT CASE b WHEN 'x' THEN 1 WHEN 'y' THEN 2 ELSE 3 END FROM t",
        Cols(&[(I, true, false)]),
    ),
    case(
        "iif",
        "SELECT iif(b IS NULL, 0, 1) FROM t",
        Cols(&[(I, true, false)]),
    ),
    case(
        "nullif is nullable even over a non-null column",
        "SELECT nullif(a, 1), nullif(b, 'x'), coalesce(nullif(b, 'x'), 'z') FROM t",
        Cols(&[(U, false, false), (U, false, false), (U, true, false)]),
    ),
    // operators
    case(
        "concatenation follows its operands",
        "SELECT b || 'x', a || 'x' FROM t",
        Cols(&[(T, false, false), (T, true, false)]),
    ),
    case(
        "arithmetic is numeric and nullable only for division",
        "SELECT a + 1, a / 1, a % 2, -a FROM t",
        Cols(&[
            (U, true, false),
            (U, false, false),
            (U, false, false),
            (U, true, false),
        ]),
    ),
    case(
        "bit operators are integers",
        "SELECT a & 1, a << 1, ~a FROM t",
        Cols(&[(I, true, false), (I, true, false), (I, true, false)]),
    ),
    case(
        "comparisons and logic are integers",
        "SELECT a IS NULL, a = 1, NOT a, a AND 1, a IN (1, 2) FROM t",
        Cols(&[(I, true, false); 5]),
    ),
    case(
        "comparison with a nullable operand",
        "SELECT b BETWEEN 'a' AND 'b' FROM t",
        Cols(&[(I, false, false)]),
    ),
    // casts
    case(
        "casts",
        "SELECT CAST(b AS TEXT), CAST(a AS INTEGER), CAST(a AS NUMERIC), CAST(b AS BLOB), CAST(a AS REAL) FROM t",
        Cols(&[
            (T, false, false),
            (I, true, false),
            (U, true, false),
            (B, false, false),
            (R, true, false),
        ]),
    ),
    // functions
    case(
        "functions that never return NULL or only for a NULL argument",
        "SELECT typeof(b), length(b), length(a), abs(a), upper(a), round(a, 2), instr(b, 'a') FROM t",
        Cols(&[
            (T, true, false),
            (I, false, false),
            (I, true, false),
            (U, true, false),
            (T, true, false),
            (R, true, false),
            (I, false, false),
        ]),
    ),
    case(
        "functions that return their argument's type",
        "SELECT substr(b, 1, 2), trim(b), replace(b, 'a', 'b'), lower(x), upper(y) FROM t, s",
        Cols(&[
            (U, false, false),
            (U, false, false),
            (T, false, false),
            (T, true, false),
            (T, false, false),
        ]),
    ),
    case(
        "random and quote",
        "SELECT random(), last_insert_rowid(), hex(b), quote(b) FROM t",
        Cols(&[
            (I, true, false),
            (I, true, false),
            (T, true, false),
            (T, true, false),
        ]),
    ),
    case(
        "an unknown function is conservative",
        "SELECT json_array(a), zeroblob(3), b LIKE 'x', date('now') FROM t",
        Cols(&[(U, false, false); 4]),
    ),
    case(
        "a scalar max has no argument count in its bytecode",
        "SELECT max(a, 1), min(a, b) FROM t",
        Cols(&[(U, false, false), (U, false, false)]),
    ),
    case(
        "a parameter is untyped",
        "SELECT ?1",
        Cols(&[(U, false, false)]),
    ),
    // joins
    case(
        "the left side of a left join keeps its proof, the right side is nullable",
        "SELECT t.a, s.x, s.id FROM t LEFT JOIN s ON s.id = t.id",
        Cols(&[(U, true, true), (T, false, false), (I, false, false)]),
    ),
    case(
        "the left table of a left join written second",
        "SELECT s.x, t.a FROM s LEFT JOIN t ON t.id = s.id",
        Cols(&[(T, true, true), (U, false, false)]),
    ),
    case(
        "coalesce over the right side of a left join",
        "SELECT coalesce(s.x, 'z'), s.x || '1', ifnull(s.y, 'q'), s.r FROM t LEFT JOIN s ON s.id = t.id",
        Cols(&[
            (T, true, false),
            (T, false, false),
            (T, true, false),
            (R, false, false),
        ]),
    ),
    case(
        "a left join that a where clause turns into an inner join",
        "SELECT t.a, s.x FROM t LEFT JOIN s ON s.id = t.id WHERE s.x IS NOT NULL",
        Cols(&[(U, true, true), (T, true, true)]),
    ),
    case(
        "a chain of left joins keeps the first table",
        "SELECT t.a FROM t LEFT JOIN s ON s.id = t.id LEFT JOIN t t2 ON t2.id = s.id",
        Cols(&[(U, true, true)]),
    ),
    case(
        "inner join",
        "SELECT t.a, s.x FROM t JOIN s ON t.id = s.id",
        Cols(&[(U, true, true), (T, true, true)]),
    ),
    case(
        "count over a left join with group by",
        "SELECT s.x, count(t.id) FROM s LEFT JOIN t ON t.id = s.id GROUP BY s.x",
        Cols(&[(U, false, false), (I, true, false)]),
    ),
    // views, subqueries and ordering
    case("view", "SELECT a FROM v", Cols(&[(U, true, true)])),
    case(
        "a subquery the flattener inlines",
        "SELECT a FROM (SELECT a FROM t)",
        Cols(&[(U, true, true)]),
    ),
    case(
        "a flattened subquery that computes",
        "SELECT a FROM (SELECT a + 1 AS a FROM t)",
        Cols(&[(U, true, false)]),
    ),
    case(
        "an in subquery that becomes a lookup",
        "SELECT a FROM t WHERE a IN (SELECT id FROM s)",
        Cols(&[(U, true, true)]),
    ),
    case(
        "order by through an index",
        "SELECT a FROM t ORDER BY b LIMIT 3 OFFSET 1",
        Cols(&[(U, true, true)]),
    ),
    case(
        "order by through the sorter",
        "SELECT a, b FROM t ORDER BY a DESC",
        Cols(&[(U, true, true), (U, false, false)]),
    ),
    case(
        "limit with a parameter",
        "SELECT a FROM t WHERE id = ?1 LIMIT ?2",
        Cols(&[(U, true, true)]),
    ),
    // columns
    case(
        "a non-strict column has an uncertain class",
        "SELECT c, b FROM t",
        Cols(&[(U, false, false), (U, false, false)]),
    ),
    case(
        "strict columns have their declared class",
        "SELECT n, r, x FROM s",
        Cols(&[(I, false, false), (R, true, true), (T, true, true)]),
    ),
    case(
        "rowid and its alias",
        "SELECT rowid, id FROM t",
        Cols(&[(I, true, true), (I, true, true)]),
    ),
    case(
        "a WITHOUT ROWID table is not mapped",
        "SELECT k, v FROM w",
        Cols(&[(U, false, false), (U, false, false)]),
    ),
    case(
        "a virtual generated column",
        "SELECT g FROM t",
        Cols(&[(U, true, false)]),
    ),
    case(
        "a covering index read",
        "SELECT y, x FROM s ORDER BY y",
        Cols(&[(T, false, false), (T, true, true)]),
    ),
    // index scans
    case(
        "a range scan",
        "SELECT a FROM big WHERE a > 5 AND c = 'x'",
        Cols(&[(U, true, true)]),
    ),
    case(
        "an open range",
        "SELECT a FROM big WHERE a > 5",
        Cols(&[(U, true, true)]),
    ),
    case(
        "a rowid range scanned backwards",
        "SELECT a FROM big WHERE rowid BETWEEN 3 AND 5 ORDER BY rowid DESC",
        Cols(&[(U, true, true)]),
    ),
    case(
        "a range with both bounds",
        "SELECT a, b FROM big WHERE a >= 1 AND a < 9 AND b > 2",
        Cols(&[(U, true, true), (U, false, false)]),
    ),
    case(
        "a descending scan",
        "SELECT a FROM big WHERE a < 5 ORDER BY a DESC",
        Cols(&[(U, true, true)]),
    ),
    case(
        "a descending scan with an upper bound on the second key",
        "SELECT a FROM big WHERE a = 1 AND b < 5 ORDER BY b DESC",
        Cols(&[(U, true, true)]),
    ),
    case(
        "a descending scan over a nullable column",
        "SELECT c FROM big WHERE c <= 'x' ORDER BY c DESC",
        Cols(&[(U, false, false)]),
    ),
    case(
        "a descending scan with a lower bound",
        "SELECT c FROM big WHERE c >= 'x' ORDER BY c DESC",
        Cols(&[(U, false, false)]),
    ),
    case(
        "max through the index",
        "SELECT max(a), min(c) FROM big",
        Cols(&[(U, false, false), (U, false, false)]),
    ),
    case(
        "an index scan beside a left join table",
        "SELECT big.a, t.b FROM big LEFT JOIN t ON t.id = big.a",
        Cols(&[(U, true, true), (U, false, false)]),
    ),
    // more operators
    case(
        "a large literal and the remaining arithmetic",
        "SELECT 9223372036854775000, a * 2, a | 1, a >> 1 FROM big",
        Cols(&[
            (I, true, false),
            (U, true, false),
            (I, true, false),
            (I, true, false),
        ]),
    ),
    case(
        "boolean operators",
        "SELECT a IS TRUE, a OR 1, b > 1 OR a > 1, b > 1 AND a > 1 FROM big",
        Cols(&[
            (I, true, false),
            (I, true, false),
            (I, false, false),
            (I, false, false),
        ]),
    ),
    case(
        "coalesce beside an aggregate",
        "SELECT count(*), coalesce(b, 0) FROM t",
        Cols(&[(I, true, false), (U, true, false)]),
    ),
    // not analysed
    case(
        "a table-valued function",
        "SELECT value FROM json_each('[1]')",
        Quiet,
    ),
    case(
        "a pragma function",
        "SELECT name FROM pragma_table_info('t')",
        Quiet,
    ),
    case("distinct", "SELECT DISTINCT a FROM t", Quiet),
    case(
        "a compound select",
        "SELECT a FROM t UNION SELECT a FROM t",
        Quiet,
    ),
    case(
        "a union all is two plain scans",
        "SELECT a FROM t UNION ALL SELECT a FROM t",
        Cols(&[(U, true, true)]),
    ),
    case(
        "a union all arm that is NULL",
        "SELECT a FROM t UNION ALL SELECT NULL",
        Cols(&[(U, false, false)]),
    ),
    case(
        "a materialized common table expression",
        "WITH c AS MATERIALIZED (SELECT a FROM t) SELECT a FROM c",
        Quiet,
    ),
    case(
        "a recursive common table expression",
        "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 3) SELECT n FROM c",
        Quiet,
    ),
    case("a window function", "SELECT count(*) OVER () FROM t", Quiet),
    case("a scalar subquery", "SELECT (SELECT max(a) FROM t)", Quiet),
    case("an in list", "SELECT a FROM t WHERE id IN (1, 2, 3)", Quiet),
    case(
        "a statement that never produces a row",
        "SELECT a FROM t WHERE 0",
        Quiet,
    ),
    case(
        "a right join",
        "SELECT t.a FROM t RIGHT JOIN s ON s.id = t.id",
        Quiet,
    ),
    case(
        "a full join",
        "SELECT t.a FROM t FULL JOIN s ON s.id = t.id",
        Quiet,
    ),
    case("an explain", "EXPLAIN SELECT 1", Quiet),
    case(
        "an insert",
        "INSERT INTO log (m) VALUES ('x') RETURNING m",
        Quiet,
    ),
    case(
        "an insert that fires a trigger",
        "INSERT INTO t (a) VALUES (1) RETURNING id, a, b",
        Quiet,
    ),
    case(
        "an update returning",
        "UPDATE log SET m = 'y' RETURNING m",
        Quiet,
    ),
    case("a delete returning", "DELETE FROM log RETURNING m", Quiet),
    case("a pragma", "PRAGMA table_info(t)", Quiet),
];

fn columns(conn: &Connection, sql: &str) -> Result<Vec<Col>, Silent> {
    Ok(analyze(conn, sql)?
        .iter()
        .map(|fact| (fact.class, fact.non_null, fact.pass_through))
        .collect())
}

#[test]
fn each_query_has_the_expected_type_and_nullability() -> TestResult {
    let conn = database()?;
    let mut wrong = Vec::new();
    for case in CASES {
        let got = columns(&conn, case.sql);
        let ok = match (&case.expect, &got) {
            (Cols(want), Ok(got)) => got.as_slice() == *want,
            (Expect::Silent, Err(_)) => true,
            _ => false,
        };
        if !ok {
            wrong.push(format!("{}: {}\n    got {got:?}", case.name, case.sql));
        }
    }
    assert!(wrong.is_empty(), "{}", wrong.join("\n"));
    Ok(())
}

#[test]
fn a_silent_statement_names_the_opcode_it_does_not_model() -> TestResult {
    let conn = database()?;
    for (sql, opcode) in [
        ("SELECT value FROM json_each('[1]')", "VOpen"),
        ("SELECT DISTINCT a FROM t", "OpenEphemeral"),
        ("SELECT count(*) OVER () FROM t", "InitCoroutine"),
        ("SELECT (SELECT max(a) FROM t)", "BeginSubrtn"),
        ("INSERT INTO log (m) VALUES ('x')", "OpenWrite"),
    ] {
        assert_eq!(
            analyze(&conn, sql),
            Err(Silent::Unmodelled(opcode.to_owned())),
            "{sql}"
        );
    }
    assert_eq!(
        analyze(&conn, "SELECT a FROM t WHERE 0"),
        Err(Silent::NoResult)
    );
    assert!(matches!(
        analyze(&conn, "SELECT nope FROM t"),
        Err(Silent::Explain(_))
    ));
    Ok(())
}

#[test]
fn an_unreadable_root_page_is_conservative_not_silent() -> TestResult {
    let conn = database()?;
    // sqlite_schema has no row of its own, so its columns are unmapped: nullable and untyped
    assert_eq!(
        columns(&conn, "SELECT name FROM sqlite_schema"),
        Ok(vec![(U, false, false)])
    );
    Ok(())
}

#[test]
fn the_analysis_reads_attached_and_temp_databases() -> TestResult {
    let conn = database()?;
    conn.execute_batch(
        "ATTACH ':memory:' AS other;
         CREATE TABLE other.o (k INTEGER NOT NULL, l TEXT) STRICT;
         CREATE TEMP TABLE tmp (k INTEGER NOT NULL, l TEXT);",
    )?;
    assert_eq!(
        columns(
            &conn,
            "SELECT coalesce(l, 'z'), k, l, count(*) FROM other.o"
        ),
        Ok(vec![
            (T, true, false),
            (I, false, false),
            (T, false, false),
            (I, true, false)
        ])
    );
    assert_eq!(
        columns(&conn, "SELECT o.k, tmp.k, tmp.l FROM other.o o, tmp"),
        Ok(vec![(I, true, true), (U, true, true), (U, false, false)])
    );
    Ok(())
}

#[test]
fn a_dropped_default_column_is_not_a_stored_slot() -> TestResult {
    let conn = Connection::open_in_memory()?;
    // g is virtual and so occupies no slot, which moves the stored column after it
    conn.execute_batch(
        "CREATE TABLE q (a INTEGER, g INTEGER GENERATED ALWAYS AS (a * 2) VIRTUAL, z TEXT NOT NULL, h TEXT NOT NULL GENERATED ALWAYS AS (z || 'h') STORED) STRICT;",
    )?;
    assert_eq!(
        columns(&conn, "SELECT coalesce(a, 0), z, h FROM q"),
        Ok(vec![(I, true, false), (T, true, true), (T, true, true)])
    );
    Ok(())
}

// the version the opcode list was reviewed for, and the opcodes the analysis relies on
#[test]
fn the_bundled_sqlite_is_the_version_the_opcodes_were_reviewed_against() {
    assert_eq!(
        rusqlite::version(),
        REVIEWED_SQLITE_VERSION,
        "SQLite changed: review MODELED_OPCODES against its EXPLAIN output, then update REVIEWED_SQLITE_VERSION"
    );
}

#[test]
fn the_modelled_opcode_list_is_unique_and_every_name_parses() {
    let names: BTreeSet<&str> = MODELED_OPCODES.iter().copied().collect();
    assert_eq!(names.len(), MODELED_OPCODES.len(), "a name is listed twice");
    for name in MODELED_OPCODES {
        assert!(Op::parse(name).is_some(), "{name}");
    }
    assert_eq!(Op::parse("OpenEphemeral"), None);
    assert_eq!(Op::ALL.len(), MODELED_OPCODES.len());
}

fn emitted(conn: &Connection, sql: &str) -> Result<Vec<(String, bool)>, Silent> {
    Ok(read_program(conn, sql)?
        .into_iter()
        .map(|insn| (insn.name, insn.op.is_some()))
        .collect())
}

#[test]
fn every_opcode_the_corpus_emits_is_modelled_or_silences_the_analysis() -> TestResult {
    let conn = database()?;
    let mut seen = BTreeSet::new();
    for case in CASES {
        let Ok(program) = emitted(&conn, case.sql) else {
            continue;
        };
        let unmodelled: Vec<&str> = program
            .iter()
            .filter(|(_, known)| !known)
            .map(|(name, _)| name.as_str())
            .collect();
        if !unmodelled.is_empty() {
            assert!(
                analyze(&conn, case.sql).is_err(),
                "{}: {unmodelled:?} reach a result",
                case.sql
            );
        }
        seen.extend(
            program
                .into_iter()
                .filter(|(_, known)| *known)
                .map(|(name, _)| name),
        );
    }
    let unexercised: Vec<&str> = MODELED_OPCODES
        .iter()
        .copied()
        .filter(|name| !seen.contains(*name))
        .collect();
    assert_eq!(
        unexercised,
        Vec::<&str>::new(),
        "no corpus statement emits these"
    );
    Ok(())
}

// What each scalar function and aggregate the analysis relies on really does, by running it.
#[test]
fn the_function_model_matches_sqlite() -> TestResult {
    let conn = Connection::open_in_memory()?;
    conn.execute_batch("CREATE TABLE e (v)")?;
    for (expression, expected) in [
        ("typeof(NULL)", "text"),
        ("quote(NULL)", "text"),
        ("hex(NULL)", "text"),
        ("sqlite_version()", "text"),
        ("random()", "integer"),
        ("changes()", "integer"),
        ("total_changes()", "integer"),
        ("last_insert_rowid()", "integer"),
        ("length(NULL)", "null"),
        ("length(x'00')", "integer"),
        ("instr(NULL, 'a')", "null"),
        ("instr('a', NULL)", "null"),
        ("instr('a', 'a')", "integer"),
        ("abs(NULL)", "null"),
        ("abs(-1)", "integer"),
        ("abs('x')", "real"),
        ("abs(x'41')", "real"),
        ("round(NULL)", "null"),
        ("round(1)", "real"),
        ("round(1.55, 1)", "real"),
        ("upper(NULL)", "null"),
        ("upper(1)", "text"),
        ("lower(x'41')", "text"),
        ("replace(NULL, 'a', 'b')", "null"),
        ("replace('a', NULL, 'b')", "null"),
        ("replace('a', 'a', NULL)", "null"),
        ("replace(1, 1, 2)", "text"),
        ("typeof(trim(x'2041'))", "text"),
        ("substr(x'4142', 1, 1)", "blob"),
        ("nullif(1, 1)", "null"),
        ("nullif(1, 2)", "integer"),
        ("1 / 0", "null"),
        ("1 % 0", "null"),
        ("1.5 / 0", "null"),
        ("x'41' || x'42'", "text"),
        ("1 || 2", "text"),
        ("NULL || 'a'", "null"),
        ("'a' + 1", "integer"),
        ("9223372036854775807 + 1", "real"),
        ("CAST(NULL AS TEXT)", "null"),
        ("CAST(1 AS BLOB)", "blob"),
        ("CAST('1' AS NUMERIC)", "integer"),
        ("CAST('1.5' AS NUMERIC)", "real"),
        ("1 & NULL", "null"),
        ("~NULL", "null"),
        ("NOT NULL", "null"),
        ("1 << 1", "integer"),
        ("(SELECT count(*) FROM e)", "integer"),
        ("(SELECT total(v) FROM e)", "real"),
        ("(SELECT sum(v) FROM e)", "null"),
        ("(SELECT avg(v) FROM e)", "null"),
        ("(SELECT max(v) FROM e)", "null"),
        ("(SELECT group_concat(v) FROM e)", "null"),
        ("(SELECT avg(1))", "real"),
        ("(SELECT group_concat(x'41'))", "text"),
        ("(SELECT sum(1))", "integer"),
        ("(SELECT sum(1.5))", "real"),
    ] {
        let got: String = conn.query_row(&format!("SELECT typeof({expression})"), [], |row| {
            row.get(0)
        })?;
        assert_eq!(got, expected, "{expression}");
    }
    Ok(())
}

fn from(tables: &[&str]) -> Structure {
    Structure {
        sources: tables.iter().map(|name| vec![(*name).to_owned()]).collect(),
        ..Structure::default()
    }
}

fn with(construct: Construct, tables: &[&str]) -> Structure {
    Structure {
        constructs: vec![construct],
        ..from(tables)
    }
}

// (kind, type name, nullable, reason, cause) of one result column
type DescribeCase = (&'static str, &'static str, Structure, Vec<Described>);

type Described = (
    Option<Kind>,
    &'static str,
    bool,
    &'static str,
    Option<&'static str>,
);

const INT_KIND: Option<Kind> = Some(Kind::Integer);
const REAL_KIND: Option<Kind> = Some(Kind::Real);
const TEXT_KIND: Option<Kind> = Some(Kind::Text);

fn expression_describe_cases() -> Vec<DescribeCase> {
    vec![
        (
            "count star is a non-null integer",
            "SELECT count(*) AS n FROM t",
            with(Construct::Aggregate, &["t"]),
            vec![(INT_KIND, "INTEGER", false, "ComputedExpr", None)],
        ),
        (
            "an aggregate that can be NULL stays unproven and untyped",
            "SELECT sum(a) AS m, max(a) AS x FROM t",
            with(Construct::Aggregate, &["t"]),
            vec![
                (None, "", true, "Unproven", Some("aggregate")),
                (None, "", true, "Unproven", Some("aggregate")),
            ],
        ),
        (
            "total is a non-null real and avg a nullable real",
            "SELECT total(a) AS t, avg(a) AS m FROM t",
            with(Construct::Aggregate, &["t"]),
            vec![
                (REAL_KIND, "REAL", false, "ComputedExpr", None),
                (REAL_KIND, "REAL", true, "Unproven", Some("aggregate")),
            ],
        ),
        (
            "a literal",
            "SELECT 'x' AS lit, 1 AS one",
            from(&[]),
            vec![
                (TEXT_KIND, "TEXT", false, "ComputedExpr", None),
                (INT_KIND, "INTEGER", false, "ComputedExpr", None),
            ],
        ),
        (
            "coalesce of a nullable column",
            "SELECT coalesce(y, 'z') AS v, ifnull(y, 'z') AS w FROM s",
            from(&["s"]),
            vec![
                (TEXT_KIND, "TEXT", false, "ComputedExpr", None),
                (TEXT_KIND, "TEXT", false, "ComputedExpr", None),
            ],
        ),
        (
            "a concatenation of a nullable column stays nullable",
            "SELECT b || 'x' AS v FROM t",
            from(&["t"]),
            vec![(TEXT_KIND, "TEXT", true, "Unproven", Some("expression"))],
        ),
        (
            "a cast has the class it names",
            "SELECT CAST(a AS TEXT) AS v FROM t",
            from(&["t"]),
            vec![(TEXT_KIND, "TEXT", false, "ComputedExpr", None)],
        ),
        (
            "arithmetic has no certain class, division can be NULL",
            "SELECT a + 1 AS p, a / 1 AS q FROM t",
            from(&["t"]),
            vec![
                (None, "", false, "ComputedExpr", None),
                (None, "", true, "Unproven", Some("expression")),
            ],
        ),
    ]
}

fn join_describe_cases() -> Vec<DescribeCase> {
    vec![
        (
            "the left of a left join keeps its column proof, the right is nullable",
            "SELECT t.a, s.x FROM t LEFT JOIN s ON t.id = s.id",
            with(Construct::OuterJoin, &["t", "s"]),
            vec![
                (INT_KIND, "INTEGER", false, "NotNullColumn", None),
                (TEXT_KIND, "TEXT", true, "Unproven", Some("outer join")),
            ],
        ),
        (
            "a view column over a NOT NULL column",
            "SELECT a, b FROM v",
            from(&["v"]),
            vec![
                (INT_KIND, "INTEGER", false, "NotNullColumn", None),
                (TEXT_KIND, "TEXT", true, "Unproven", Some("view v")),
            ],
        ),
        (
            "a flattened subquery over a NOT NULL column",
            "SELECT a FROM (SELECT a FROM t)",
            with(Construct::Subquery, &["t"]),
            vec![(INT_KIND, "INTEGER", false, "NotNullColumn", None)],
        ),
        (
            "the existing verdicts do not change",
            "SELECT id, a, b, c FROM t",
            from(&["t"]),
            vec![
                (INT_KIND, "INTEGER", false, "NotNullColumn", None),
                (INT_KIND, "INTEGER", false, "NotNullColumn", None),
                (TEXT_KIND, "TEXT", true, "NullableColumn", None),
                (None, "", true, "NullableColumn", None),
            ],
        ),
        (
            "a non-strict column read has no inferred class",
            "SELECT coalesce(c, 1) AS v FROM t",
            from(&["t"]),
            vec![(None, "", false, "ComputedExpr", None)],
        ),
        (
            "an unknown function is conservative",
            "SELECT json_array(a) AS j FROM t",
            from(&["t"]),
            vec![(None, "", true, "Unproven", Some("expression"))],
        ),
        (
            "a bang override still wins",
            "SELECT count(*) AS \"n!\", sum(a) AS \"m!\" FROM t",
            with(Construct::Aggregate, &["t"]),
            vec![
                (INT_KIND, "INTEGER", false, "OverrideBang", None),
                (None, "", false, "OverrideBang", None),
            ],
        ),
        (
            "a question override still wins over a proof",
            "SELECT count(*) AS \"n?\" FROM t",
            with(Construct::Aggregate, &["t"]),
            vec![(INT_KIND, "INTEGER", true, "OverrideQuestion", None)],
        ),
        (
            "a table-valued function is not analysed",
            "SELECT value FROM json_each('[1]')",
            from(&["json_each"]),
            vec![(
                None,
                "",
                true,
                "Unproven",
                Some("source json_each is not a table"),
            )],
        ),
        (
            "a compound select that needs an ephemeral table is not analysed",
            "SELECT a FROM t UNION SELECT a FROM t",
            with(Construct::CompoundSelect, &["t"]),
            vec![(
                INT_KIND,
                "INTEGER",
                true,
                "Unproven",
                Some("compound select"),
            )],
        ),
        (
            "a returning clause reports the table's columns",
            "INSERT INTO t (a) VALUES (1) RETURNING id, a, b",
            from(&["t"]),
            vec![
                (INT_KIND, "INTEGER", false, "NotNullColumn", None),
                (INT_KIND, "INTEGER", false, "NotNullColumn", None),
                (TEXT_KIND, "TEXT", true, "NullableColumn", None),
            ],
        ),
    ]
}

#[test]
fn describe_applies_the_bytecode_only_to_unproven_columns() -> TestResult {
    let conn = database()?;
    let mut wrong = Vec::new();
    for (name, sql, structure, want) in expression_describe_cases()
        .into_iter()
        .chain(join_describe_cases())
    {
        let got = match describe(&conn, sql, &structure) {
            Ok(described) => described
                .columns
                .iter()
                .map(|col| {
                    (
                        col.kind,
                        col.type_name.clone(),
                        col.nullable,
                        col.reason.name().to_owned(),
                        col.reason.detail().map(str::to_owned),
                    )
                })
                .collect::<Vec<_>>(),
            Err(err) => {
                wrong.push(format!("{name}: {err}"));
                continue;
            }
        };
        let want: Vec<_> = want
            .into_iter()
            .map(|(kind, type_name, nullable, reason, cause)| {
                (
                    kind,
                    type_name.to_owned(),
                    nullable,
                    reason.to_owned(),
                    cause.map(str::to_owned),
                )
            })
            .collect();
        if got != want {
            wrong.push(format!(
                "{name}: {sql}\n    got  {got:?}\n    want {want:?}"
            ));
        }
    }
    assert!(wrong.is_empty(), "{}", wrong.join("\n"));
    Ok(())
}

#[test]
fn describing_an_explain_statement_never_reads_its_bytecode() -> TestResult {
    let conn = database()?;
    let described = describe(&conn, "EXPLAIN SELECT 1", &Structure::default())?;
    assert!(
        described
            .columns
            .iter()
            .all(|col| col.reason.name() != "ComputedExpr")
    );
    Ok(())
}

#[test]
fn describing_never_runs_the_statement() -> TestResult {
    let conn = database()?;
    for sql in [
        "INSERT INTO log (m) VALUES ('x') RETURNING m",
        "UPDATE log SET m = 'y' RETURNING m",
        "DELETE FROM log RETURNING m",
        "SELECT count(*) FROM log",
    ] {
        describe(&conn, sql, &Structure::default())?;
    }
    let rows: i64 = conn.query_row("SELECT count(*) FROM log", [], |row| row.get(0))?;
    assert_eq!(rows, 0);
    assert_eq!(conn.total_changes(), 0);
    Ok(())
}

#[test]
fn a_bare_column_beside_an_index_max_stays_nullable() -> TestResult {
    let conn = database()?;
    // max(b) is read by seeking the index; over an empty table the bare column a reads NULL
    assert_eq!(
        columns(&conn, "SELECT a, max(b) FROM big"),
        Ok(vec![(U, false, false), (U, false, false)])
    );
    assert_eq!(
        columns(&conn, "SELECT a, min(b) FROM big"),
        Ok(vec![(U, false, false), (U, false, false)])
    );
    Ok(())
}

fn rows_after(sql: &str, op: Op, jump: bool) -> Result<Option<u8>, Silent> {
    let conn = database().map_err(|err| Silent::Explain(err.to_string()))?;
    let program = read_program(&conn, sql)?;
    let layouts = super::read_layouts(&conn, &program);
    let machine = super::Machine {
        program: &program,
        layouts,
    };
    let Some(pc) = program.iter().position(|insn| insn.op == Some(op)) else {
        return Ok(None);
    };
    let mut state = super::State::default();
    state.set_rows(0, super::ROW);
    let edges = machine.step(pc, &state)?;
    let target = if jump {
        program[pc].p2
    } else {
        i64::try_from(pc + 1).unwrap_or(-1)
    };
    Ok(edges
        .iter()
        .find(|(addr, _)| i64::try_from(*addr) == Ok(target))
        .and_then(|(_, edge)| edge.cursors.get(&program[pc].p1).map(|c| c.rows)))
}

#[test]
fn the_no_row_edge_of_a_cursor_move_may_be_a_null_row() -> TestResult {
    let both = super::ROW | super::NULL_ROW;
    // Rewind and the seeks: falling through finds a row, jumping does not
    assert_eq!(
        rows_after("SELECT a FROM t", Op::Rewind, false)?,
        Some(super::ROW)
    );
    assert_eq!(rows_after("SELECT a FROM t", Op::Rewind, true)?, Some(both));
    assert_eq!(
        rows_after("SELECT a FROM t WHERE id = 1", Op::SeekRowid, false)?,
        Some(super::ROW)
    );
    assert_eq!(
        rows_after("SELECT a FROM t WHERE id = 1", Op::SeekRowid, true)?,
        Some(both)
    );
    // Next: jumping back finds a row, falling through is the end
    assert_eq!(
        rows_after("SELECT a FROM t", Op::Next, true)?,
        Some(super::ROW)
    );
    assert_eq!(rows_after("SELECT a FROM t", Op::Next, false)?, Some(both));
    Ok(())
}

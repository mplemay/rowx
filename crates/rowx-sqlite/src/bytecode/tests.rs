use std::cell::Cell;
use std::collections::BTreeSet;

use rusqlite::Connection;
use rusqlite::types::ValueRef;

use super::{Class, MODELED_OPCODES, Op, REVIEWED_SQLITE_VERSION, Silent, analyze, read_program};
use crate::describe::{ANALYSES, Construct, Structure, describe};
use crate::types::Kind;

const SCHEMA: &str = "
    CREATE TABLE t (id INTEGER PRIMARY KEY, a INTEGER NOT NULL, b TEXT, c,
        g INTEGER GENERATED ALWAYS AS (a + 1) VIRTUAL);
    CREATE TABLE s (id INTEGER PRIMARY KEY, x TEXT NOT NULL, y TEXT, r REAL NOT NULL, n INTEGER) STRICT;
    CREATE TABLE w (k INTEGER PRIMARY KEY, v TEXT) WITHOUT ROWID;
    CREATE TABLE log (m TEXT);
    CREATE TABLE nr (r REAL NOT NULL, r2 REAL NOT NULL, i INTEGER NOT NULL, j INTEGER NOT NULL) STRICT;
    CREATE TABLE big (a INTEGER NOT NULL, b INTEGER, c TEXT);
    CREATE TABLE u (k INTEGER PRIMARY KEY, uq TEXT UNIQUE, d INTEGER NOT NULL DEFAULT 5, ck INTEGER CHECK (ck > 0));
    CREATE TABLE fk (id INTEGER PRIMARY KEY, tid INTEGER REFERENCES t (id));
    CREATE TABLE fk_cascade (id INTEGER PRIMARY KEY, tid INTEGER REFERENCES t (id) ON DELETE CASCADE);
    CREATE INDEX big_ab ON big (a, b);
    CREATE INDEX big_c ON big (c);
    CREATE VIEW v AS SELECT a, b FROM t;
    CREATE VIEW vc AS SELECT a, count(*) AS n FROM t GROUP BY a;
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
        "count is never NULL, total may be NaN",
        "SELECT count(b), total(a) FROM t",
        Cols(&[(I, true, false), (R, false, false)]),
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
            (R, false, false),
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
        "a real sum, difference or product may be NaN, which is NULL",
        "SELECT r + r2, r - r2, r * r2, r * 0, r * i, r + i, i - r, i + j, i - j, i * j, i * 2 FROM nr",
        Cols(&[
            (U, false, false),
            (U, false, false),
            (U, false, false),
            (U, false, false),
            (U, false, false),
            (U, true, false),
            (U, true, false),
            (U, true, false),
            (U, true, false),
            (U, true, false),
            (U, true, false),
        ]),
    ),
    case(
        "an integer primary key plus a literal is never NULL",
        "SELECT id + 1, id - 1, 1 + id FROM s",
        Cols(&[(U, true, false), (U, true, false), (U, true, false)]),
    ),
    case(
        "total may be NaN even over a real column",
        "SELECT total(r) FROM nr",
        Cols(&[(R, false, false)]),
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
        Cols(&[(T, false, false), (I, true, false)]),
    ),
    // views, subqueries and ordering
    case("view", "SELECT a FROM v", Cols(&[(U, true, true)])),
    case(
        "an unflattened view",
        "SELECT n FROM vc",
        Cols(&[(I, true, false)]),
    ),
    case(
        "an unflattened view, two columns",
        "SELECT a, n FROM vc",
        Cols(&[(U, false, false), (I, true, false)]),
    ),
    case(
        "an unflattened view on the nullable side of a left join",
        "SELECT vc.n FROM t LEFT JOIN vc ON vc.a = t.a",
        Cols(&[(I, false, false)]),
    ),
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
            // the column is not certainly an integer, so the product may be NaN
            (U, false, false),
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
    case(
        "a statement that never produces a row",
        "SELECT a FROM t WHERE 0",
        Quiet,
    ),
    case("an explain", "EXPLAIN SELECT 1", Quiet),
    case("a pragma", "PRAGMA table_info(t)", Quiet),
    case("a window function", "SELECT count(*) OVER () FROM t", Quiet),
    case(
        "a ranking window function",
        "SELECT row_number() OVER (ORDER BY a) FROM t",
        Quiet,
    ),
    case(
        "a window frame that removes rows",
        "SELECT sum(a) OVER (ORDER BY id ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) FROM t",
        Quiet,
    ),
    case(
        "first_value over a frame of the following rows",
        "SELECT first_value(a) OVER (ORDER BY a ROWS BETWEEN 1 FOLLOWING AND 2 FOLLOWING) FROM t",
        Cols(&[(U, false, false)]),
    ),
    case(
        "first_value over a frame of the preceding rows",
        "SELECT first_value(a) OVER (ORDER BY a ROWS BETWEEN 2 PRECEDING AND 1 PRECEDING) FROM t",
        Cols(&[(U, false, false)]),
    ),
    case(
        "nth_value past the start of the frame",
        "SELECT nth_value(a, 2) OVER (ORDER BY a) FROM t",
        Cols(&[(U, false, false)]),
    ),
    case(
        "first_value over the whole table",
        "SELECT first_value(a) OVER () FROM t",
        Cols(&[(U, false, false)]),
    ),
    case(
        "a window function in a subquery",
        "SELECT x FROM (SELECT x, row_number() OVER () AS rn FROM s) WHERE rn = 1",
        Quiet,
    ),
    case(
        "a window function in a common table expression",
        "WITH w AS (SELECT a, rank() OVER (ORDER BY a) r FROM t) SELECT r FROM w",
        Quiet,
    ),
    case(
        "a window function behind an in subquery",
        "SELECT a FROM t WHERE a IN (SELECT row_number() OVER () FROM s)",
        Quiet,
    ),
    case(
        "a window function that returns a return address",
        "SELECT a, count(*) OVER (PARTITION BY a) FROM t",
        Quiet,
    ),
    case(
        "an order by that needs a sort of its own",
        "SELECT a FROM t UNION ALL SELECT id FROM s ORDER BY 1 LIMIT 2",
        Quiet,
    ),
    case(
        "a right join",
        "SELECT t.a FROM t RIGHT JOIN s ON s.id = t.id",
        Cols(&[(U, false, false)]),
    ),
    case(
        "the preserved side of a right join keeps its proof",
        "SELECT s.x FROM t RIGHT JOIN s ON s.id = t.id",
        Cols(&[(T, true, true)]),
    ),
    case(
        "a full join",
        "SELECT t.a, s.x FROM t FULL JOIN s ON s.id = t.id",
        Cols(&[(U, false, false), (T, false, false)]),
    ),
    // flat compound selects and choices between columns
    case(
        "a union all is two plain scans",
        "SELECT a FROM t UNION ALL SELECT a FROM t",
        Cols(&[(U, true, true)]),
    ),
    case(
        "a union all of two different columns is not a pass through",
        "SELECT a FROM t UNION ALL SELECT a FROM big",
        Cols(&[(U, true, false)]),
    ),
    case(
        "a case choosing between two columns is not a pass through",
        "SELECT CASE WHEN a > 1 THEN a ELSE id END FROM t",
        Cols(&[(U, true, false)]),
    ),
    case(
        "two different strict columns of one class keep the class but are not a pass through",
        "SELECT id FROM s UNION ALL SELECT n FROM s",
        Cols(&[(I, false, false)]),
    ),
    case(
        "a union all of two strict columns of one class keeps the class",
        "SELECT x FROM s UNION ALL SELECT coalesce(y, x) FROM s",
        Cols(&[(T, true, false)]),
    ),
    case(
        "a union all arm that is NULL",
        "SELECT a FROM t UNION ALL SELECT NULL",
        Cols(&[(U, false, false)]),
    ),
    // ephemeral tables: distinct, in, materialized views
    case(
        "distinct over a NOT NULL column keeps the proof",
        "SELECT DISTINCT a FROM t",
        Cols(&[(U, true, true)]),
    ),
    case(
        "distinct over a nullable column",
        "SELECT DISTINCT a, b FROM t",
        Cols(&[(U, true, true), (U, false, false)]),
    ),
    case(
        "distinct over an expression",
        "SELECT DISTINCT a + 1 FROM t",
        Cols(&[(U, true, false)]),
    ),
    case(
        "a materialized common table expression",
        "WITH c AS MATERIALIZED (SELECT a FROM t) SELECT a FROM c",
        Cols(&[(U, true, true)]),
    ),
    case(
        "a materialized common table expression of a computed column",
        "WITH c AS MATERIALIZED (SELECT a + 1 AS x FROM t) SELECT x FROM c",
        Cols(&[(U, true, false)]),
    ),
    case(
        "a materialized common table expression of a nullable column",
        "WITH c AS MATERIALIZED (SELECT b AS x FROM t) SELECT x, coalesce(x, 'z') FROM c",
        Cols(&[(U, false, false), (U, true, false)]),
    ),
    case(
        "a common table expression used twice is read through a duplicate cursor",
        "WITH c AS (SELECT a, count(*) n FROM t GROUP BY a) SELECT c1.a, c2.n FROM c c1 JOIN c c2 ON c1.a = c2.a",
        Cols(&[(U, false, false), (I, true, false)]),
    ),
    case(
        "a left join against a subquery keeps the right side nullable",
        "SELECT t.a, q.n FROM t LEFT JOIN (SELECT a, count(*) n FROM t GROUP BY a LIMIT 3) q ON q.a = t.a",
        Cols(&[(U, true, true), (I, false, false)]),
    ),
    case(
        "a left join against a materialized common table expression",
        "WITH c AS MATERIALIZED (SELECT id, x FROM s) SELECT t.a, c.x FROM t LEFT JOIN c ON c.id = t.id",
        Cols(&[(U, true, true), (T, false, false)]),
    ),
    case(
        "an in list",
        "SELECT a FROM t WHERE id IN (1, 2, 3)",
        Cols(&[(U, true, true)]),
    ),
    case(
        "an in list is an integer",
        "SELECT a IN (1, 2, 3) FROM t",
        Cols(&[(I, true, false)]),
    ),
    case(
        "an in subquery that filters",
        "SELECT a FROM t WHERE a IN (SELECT a FROM t WHERE b IS NOT NULL)",
        Cols(&[(U, true, true)]),
    ),
    // compound selects
    case(
        "a union is distinct, the arms agree it is never NULL",
        "SELECT a FROM t UNION SELECT a FROM t",
        Cols(&[(U, true, true)]),
    ),
    case(
        "a union whose arms disagree on the class has none",
        "SELECT a FROM t UNION SELECT x FROM s",
        Cols(&[(U, true, false)]),
    ),
    case(
        "a union of integers and reals has no certain class",
        "SELECT 1 UNION SELECT 2.5",
        Cols(&[(U, true, false)]),
    ),
    case(
        "a union all of two integer literals",
        "SELECT 1 UNION ALL SELECT 2",
        Cols(&[(I, true, false)]),
    ),
    case(
        "a union all with a NULL arm is nullable",
        "SELECT 1 UNION ALL SELECT NULL",
        Cols(&[(I, false, false)]),
    ),
    case(
        "a union all of a literal and a strict column",
        "SELECT 'a' UNION ALL SELECT x FROM s",
        Cols(&[(T, true, false)]),
    ),
    case(
        "a compound where one arm is nullable is nullable",
        "SELECT a FROM t UNION ALL SELECT b FROM t",
        Cols(&[(U, false, false)]),
    ),
    case(
        "a union is nullable when its last arm is",
        "SELECT id FROM s UNION SELECT n FROM s",
        Cols(&[(I, false, false)]),
    ),
    case(
        "three arms",
        "SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT n FROM s",
        Cols(&[(I, false, false)]),
    ),
    case(
        "an intersect is read from its left arm",
        "SELECT a FROM t INTERSECT SELECT x FROM s",
        Cols(&[(U, true, true)]),
    ),
    case(
        "an intersect of a nullable left arm",
        "SELECT b FROM t INTERSECT SELECT y FROM s",
        Cols(&[(U, false, false)]),
    ),
    case(
        "an except keeps what is left of its left arm",
        "SELECT a FROM t EXCEPT SELECT n FROM s",
        Cols(&[(U, true, true)]),
    ),
    case(
        "an except of a nullable left arm",
        "SELECT b FROM t EXCEPT SELECT id FROM s",
        Cols(&[(U, false, false)]),
    ),
    case(
        "a union all ordered through two coroutines",
        "SELECT a FROM t UNION ALL SELECT id FROM s ORDER BY 1",
        Cols(&[(U, true, false)]),
    ),
    case(
        "a union as a subquery",
        "SELECT * FROM (SELECT a FROM t UNION SELECT id FROM s)",
        Cols(&[(U, true, false)]),
    ),
    case(
        "a compound common table expression",
        "WITH c AS (SELECT a, b FROM t UNION SELECT id, x FROM s) SELECT a, b FROM c",
        Cols(&[(U, true, false), (U, false, false)]),
    ),
    // recursion: the queue is written after it is read, and the result is the join of every row that went in
    case(
        "a recursive common table expression",
        "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 3) SELECT n FROM c",
        Cols(&[(U, true, false)]),
    ),
    case(
        "a recursive step that writes NULL later is nullable",
        "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT NULL FROM c LIMIT 3) SELECT n FROM c",
        Cols(&[(I, false, false)]),
    ),
    // subqueries that are not flattened run as coroutines
    case(
        "a subquery with a limit and an aggregate",
        "SELECT * FROM (SELECT a, count(*) c FROM t GROUP BY a LIMIT 5)",
        Cols(&[(U, false, false), (I, true, false)]),
    ),
    case(
        "a subquery with a limit keeps the aggregate alone",
        "SELECT n FROM (SELECT count(*) AS n FROM t GROUP BY b LIMIT 5)",
        Cols(&[(I, true, false)]),
    ),
    case(
        "a subquery ordered with a limit",
        "SELECT a FROM (SELECT a FROM t ORDER BY b LIMIT 3)",
        Cols(&[(U, true, true)]),
    ),
    case(
        "a distinct subquery",
        "SELECT count(*) FROM (SELECT DISTINCT a FROM t)",
        Cols(&[(I, true, false)]),
    ),
    // scalar subqueries, exists and in run as subroutines
    case(
        "a scalar subquery over an aggregate that can be NULL",
        "SELECT (SELECT max(a) FROM t)",
        Cols(&[(U, false, false)]),
    ),
    case(
        "a scalar count is never NULL",
        "SELECT (SELECT count(*) FROM t)",
        Cols(&[(I, true, false)]),
    ),
    case(
        "arithmetic over a scalar count",
        "SELECT (SELECT count(*) FROM t) + 1",
        Cols(&[(U, true, false)]),
    ),
    case(
        "a scalar total may be NaN",
        "SELECT (SELECT total(a) FROM t)",
        Cols(&[(R, false, false)]),
    ),
    case(
        "a scalar subquery that can return no row is NULL",
        "SELECT (SELECT a FROM t WHERE id = 1)",
        Cols(&[(U, false, false)]),
    ),
    case(
        "a correlated scalar subquery",
        "SELECT t.a, (SELECT x FROM s WHERE s.id = t.id) FROM t",
        Cols(&[(U, true, true), (T, false, false)]),
    ),
    case(
        "a correlated aggregate",
        "SELECT (SELECT max(s.id) FROM s WHERE s.x = t.b) FROM t",
        Cols(&[(U, false, false)]),
    ),
    case(
        "exists is never NULL",
        "SELECT EXISTS (SELECT 1 FROM t)",
        Cols(&[(I, true, false)]),
    ),
    case(
        "a correlated exists filter",
        "SELECT t.a FROM t WHERE EXISTS (SELECT 1 FROM s WHERE s.id = t.id)",
        Cols(&[(U, true, true)]),
    ),
    case(
        "in a rowid subquery is never NULL for a NOT NULL operand",
        "SELECT a IN (SELECT id FROM s) FROM t",
        Cols(&[(I, true, false)]),
    ),
    case(
        "not in a rowid subquery",
        "SELECT a NOT IN (SELECT id FROM s) FROM t",
        Cols(&[(I, true, false)]),
    ),
    case(
        "in a subquery of a nullable column is NULL when the operand is not found",
        "SELECT a IN (SELECT n FROM s) FROM t",
        Cols(&[(I, false, false)]),
    ),
    case(
        "in a subquery with a nullable operand",
        "SELECT b IN (SELECT x FROM s) FROM t",
        Cols(&[(I, false, false)]),
    ),
    case(
        "in a subquery of a NOT NULL column the bytecode cannot rule out an equal NULL result",
        "SELECT a IN (SELECT i FROM nr) FROM t",
        Cols(&[(I, false, false)]),
    ),
    // DML with RETURNING
    case(
        "an insert returning",
        "INSERT INTO log (m) VALUES ('x') RETURNING m, rowid, 1",
        Cols(&[(U, true, false), (I, true, false), (I, true, false)]),
    ),
    case(
        "an insert returning a NULL",
        "INSERT INTO log (m) VALUES (NULL) RETURNING m",
        Cols(&[(U, false, false)]),
    ),
    case(
        "an insert of two rows, one NULL",
        "INSERT INTO log (m) VALUES ('a'), (NULL) RETURNING m",
        Cols(&[(U, false, false)]),
    ),
    case(
        "an insert from a select",
        "INSERT INTO log SELECT x FROM s RETURNING m",
        Cols(&[(U, true, false)]),
    ),
    case(
        "an insert from a nullable select",
        "INSERT INTO log (m) SELECT b FROM t RETURNING m",
        Cols(&[(U, false, false)]),
    ),
    case(
        "an insert into a strict table",
        "INSERT INTO s (x, r) VALUES ('a', 1.5) RETURNING id, x, r, n",
        Cols(&[
            (I, true, false),
            (U, true, false),
            (U, true, false),
            (U, false, false),
        ]),
    ),
    case(
        "an insert into a table with a unique index, a default and a check",
        "INSERT INTO u (uq, ck) VALUES ('a', 1) RETURNING k, uq, d, ck",
        Cols(&[
            (I, true, false),
            (U, true, false),
            (U, true, false),
            (U, true, false),
        ]),
    ),
    case(
        "an upsert",
        "INSERT INTO u (uq, ck) VALUES ('a', 1) ON CONFLICT (uq) DO UPDATE SET d = d + 1 RETURNING d",
        Cols(&[(U, true, false)]),
    ),
    case(
        "an insert or replace",
        "INSERT OR REPLACE INTO u (uq, ck) VALUES ('a', 1) RETURNING d",
        Cols(&[(U, true, false)]),
    ),
    case(
        "an insert into a without rowid table",
        "INSERT INTO w (k, v) VALUES (1, 'x') RETURNING k, v",
        Cols(&[(U, true, false), (U, true, false)]),
    ),
    case(
        "an update returning",
        "UPDATE log SET m = 'y' RETURNING m, m || 'z'",
        Cols(&[(U, true, false), (T, true, false)]),
    ),
    case(
        "an update that sets NULL",
        "UPDATE log SET m = NULL RETURNING m",
        Cols(&[(U, false, false)]),
    ),
    case(
        "an update through coalesce",
        "UPDATE log SET m = coalesce(m, 'z') RETURNING m",
        Cols(&[(U, true, false)]),
    ),
    case(
        "an update of a unique column",
        "UPDATE u SET uq = 'b' WHERE k > 2 RETURNING uq, d",
        Cols(&[(U, true, false), (U, true, false)]),
    ),
    case(
        "an update of a nullable strict column",
        "UPDATE s SET n = n + 1 WHERE id = 3 RETURNING n, n + 1",
        Cols(&[(U, false, false), (U, false, false)]),
    ),
    case(
        "a delete returning",
        "DELETE FROM log RETURNING m, rowid",
        Cols(&[(U, false, false), (I, true, false)]),
    ),
    case(
        "a delete through an index returning",
        "DELETE FROM u WHERE uq = 'a' RETURNING k, uq",
        Cols(&[(I, true, true), (U, false, false)]),
    ),
    case(
        "a delete from an indexed table",
        "DELETE FROM big WHERE a > 3 RETURNING a, b",
        Cols(&[(U, true, true), (U, false, false)]),
    ),
    // a trigger, a foreign key or a virtual table keeps the analysis silent
    case(
        "an insert that fires a trigger",
        "INSERT INTO t (a) VALUES (1) RETURNING id, a, b",
        Quiet,
    ),
    case(
        "a delete that fires a cascade",
        "DELETE FROM t RETURNING a",
        Quiet,
    ),
    case(
        "an insert into a foreign key child",
        "INSERT INTO fk (tid) VALUES (1) RETURNING id, tid",
        Quiet,
    ),
    case(
        "an update of a foreign key child",
        "UPDATE fk SET tid = 2 RETURNING tid",
        Quiet,
    ),
    case(
        "a delete from a foreign key child",
        "DELETE FROM fk RETURNING tid",
        Quiet,
    ),
    case(
        "an update of a referenced key",
        "UPDATE t SET id = 5 RETURNING id",
        Quiet,
    ),
];

fn columns(conn: &Connection, sql: &str) -> Result<Vec<Col>, Silent> {
    Ok(analyze(conn, sql, &|| false)?
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

// rows that give every proven column something to be NULL, empty or of another class
const ROWS: &str = "
    INSERT INTO t (id, a, b, c) VALUES (1, 10, 'x', 1), (2, 20, NULL, NULL), (3, 30, 'y', 2.5), (4, 10, 'x', 'text');
    INSERT INTO s (id, x, y, r, n) VALUES (1, 'p', 'q', 1.5, NULL), (2, 'r', NULL, 2.0, 7), (3, 'x', NULL, 3.5, 3);
    INSERT INTO w (k, v) VALUES (1, NULL), (2, 'z');
    INSERT INTO log (m) VALUES (NULL), ('a');
    INSERT INTO nr (r, r2, i, j) VALUES (1.5, 2.5, 1, 2), (3.0, 4.0, 5, 6);
    INSERT INTO big (a, b, c) VALUES (1, NULL, 'x'), (2, 3, NULL), (5, 6, 'y'), (9, NULL, NULL);
    INSERT INTO u (k, uq, d, ck) VALUES (1, 'a', 5, 1), (2, 'b', 6, 2), (3, NULL, 7, 3), (4, NULL, 8, NULL);
";

const fn class_of(value: ValueRef<'_>) -> Option<Class> {
    match value {
        ValueRef::Null => None,
        ValueRef::Integer(_) => Some(Class::Integer),
        ValueRef::Real(_) => Some(Class::Real),
        ValueRef::Text(_) => Some(Class::Text),
        ValueRef::Blob(_) => Some(Class::Blob),
    }
}

// Runs the statement and checks every value of every row it returns against what the analysis proved. The
// statement is rolled back, and a statement that fails (a constraint, say) has returned the rows it did. The
// count is how many rows were checked.
fn rows_break_the_proof(
    conn: &Connection,
    sql: &str,
    want: &[Col],
) -> rusqlite::Result<(Vec<String>, usize)> {
    conn.execute_batch("SAVEPOINT probe")?;
    let mut broken = Vec::new();
    let mut seen = 0;
    let mut run = || -> rusqlite::Result<()> {
        let mut stmt = conn.prepare(sql)?;
        let mut rows = stmt.query([])?;
        while let Some(row) = rows.next()? {
            seen += 1;
            for (index, (class, non_null, _)) in want.iter().enumerate() {
                let value = row.get_ref(index)?;
                if *non_null && value == ValueRef::Null {
                    broken.push(format!("{sql}: column {index} was NULL"));
                }
                if let (Some(want), Some(got)) = (class, class_of(value))
                    && *want != got
                {
                    broken.push(format!("{sql}: column {index} was {got:?}, not {want:?}"));
                }
            }
        }
        Ok(())
    };
    // a statement error is tolerated, and `seen` says how many rows were checked before it
    let _ = run();
    conn.execute_batch("ROLLBACK TO probe; RELEASE probe")?;
    Ok((broken, seen))
}

#[test]
fn every_proof_in_the_corpus_holds_for_the_rows_the_statements_return() -> TestResult {
    let empty = database()?;
    let full = database()?;
    full.execute_batch(ROWS)?;
    let mut broken = Vec::new();
    let mut checked = 0;
    for case in CASES {
        let Cols(want) = &case.expect else { continue };
        // a parameter would have to be bound to something
        if case.sql.contains('?') {
            continue;
        }
        for conn in [&empty, &full] {
            checked += 1;
            broken.extend(rows_break_the_proof(conn, case.sql, want)?.0);
        }
    }
    assert!(checked > 100, "{checked}");
    assert!(broken.is_empty(), "{}", broken.join("\n"));
    // the check does see a proof that is wrong
    let (lie, _) = rows_break_the_proof(
        &full,
        "SELECT b, c FROM t",
        &[(T, true, false), (I, false, false)],
    )?;
    assert_eq!(lie.len(), 3, "{lie:?}");
    Ok(())
}

// statements with no expected proof: whatever the analysis proves for them must hold when they run
const PROBES: &[&str] = &[
    "SELECT DISTINCT b FROM t",
    "SELECT DISTINCT c, b FROM t",
    "SELECT DISTINCT y FROM s UNION SELECT NULL",
    "SELECT n FROM s WHERE n IS NOT NULL UNION SELECT NULL WHERE 0",
    "SELECT n FROM s EXCEPT SELECT NULL",
    "SELECT b FROM t EXCEPT SELECT x FROM s",
    "SELECT y FROM s INTERSECT SELECT NULL",
    "SELECT a FROM t INTERSECT SELECT a FROM t",
    "SELECT b FROM t UNION SELECT NULL",
    "SELECT 1 UNION ALL SELECT 2 UNION SELECT NULL",
    "SELECT 1, 'a' UNION ALL SELECT 2.5, NULL",
    "SELECT * FROM (SELECT a, b FROM t UNION ALL SELECT id, y FROM s)",
    "SELECT * FROM (SELECT a, count(*) FROM t GROUP BY a ORDER BY 1 LIMIT 2 OFFSET 1)",
    "SELECT x, n FROM (SELECT x, n FROM s LIMIT 2)",
    "SELECT q.m FROM (SELECT max(a) AS m FROM t WHERE a > 100) q",
    "SELECT q.m, q.n FROM (SELECT max(a) AS m, min(b) AS n FROM t GROUP BY id LIMIT 3) q",
    "SELECT (SELECT n FROM s WHERE id = t.id) FROM t",
    "SELECT (SELECT max(n) FROM s) FROM t",
    "SELECT (SELECT n FROM s ORDER BY id LIMIT 1)",
    "SELECT (SELECT a FROM t WHERE a > 100)",
    "SELECT (SELECT a FROM t WHERE a > 100) IS NULL",
    "SELECT coalesce((SELECT max(n) FROM s WHERE id > 100), 0)",
    "SELECT EXISTS (SELECT 1 FROM t WHERE a > 100)",
    "SELECT NOT EXISTS (SELECT 1 FROM t WHERE b IS NULL)",
    "SELECT a, a IN (SELECT n FROM s) FROM t",
    "SELECT b, b IN (SELECT y FROM s) FROM t",
    "SELECT b, b NOT IN (SELECT y FROM s) FROM t",
    "SELECT a, a NOT IN (SELECT n FROM s) FROM t",
    "SELECT a, a IN (SELECT i FROM nr) FROM t",
    "SELECT a, a IN (SELECT n FROM s WHERE 0) FROM t",
    "SELECT b, b IN (SELECT y FROM s WHERE 0) FROM t",
    "SELECT a, (SELECT count(*) FROM s WHERE s.n = t.a) FROM t",
    "SELECT t.a, c.x FROM t LEFT JOIN (SELECT id, x FROM s GROUP BY id LIMIT 4) c ON c.id = t.id",
    "SELECT t.a, c.n FROM t LEFT JOIN (SELECT id, n FROM s ORDER BY n LIMIT 4) c ON c.id = t.id",
    "SELECT c.x, t.a FROM (SELECT id, x FROM s LIMIT 4) c LEFT JOIN t ON c.id = t.id",
    "WITH c AS MATERIALIZED (SELECT id, n FROM s) SELECT t.a, c.n FROM t LEFT JOIN c ON c.id = t.id",
    "WITH c AS MATERIALIZED (SELECT id, n FROM s) SELECT c1.n, c2.n FROM c c1 LEFT JOIN c c2 ON c2.id = c1.id + 1",
    "WITH c AS (SELECT id, n FROM s GROUP BY id) SELECT c1.n, c2.id FROM c c1 LEFT JOIN c c2 ON c2.n = c1.n",
    "WITH c AS (SELECT b, count(*) n FROM t GROUP BY b) SELECT c1.b, c2.n FROM c c1 JOIN c c2 ON c1.n = c2.n",
    "WITH c AS (SELECT a FROM t WHERE b IS NOT NULL) SELECT a FROM c UNION ALL SELECT a FROM c",
    "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 5) SELECT n, n * 2 FROM c",
    "WITH RECURSIVE c(n, m) AS (SELECT 1, NULL UNION ALL SELECT n + 1, n FROM c WHERE n < 4) SELECT n, m FROM c",
    "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n * 9223372036854775807 FROM c WHERE n < 3) SELECT n FROM c",
    "WITH RECURSIVE c(n) AS (SELECT 'a' UNION SELECT n || 'a' FROM c WHERE length(n) < 3) SELECT n FROM c",
    "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT CASE WHEN n < 3 THEN n + 1 END FROM c LIMIT 6) SELECT n FROM c",
    "WITH RECURSIVE c(a) AS (SELECT a FROM t UNION SELECT a + 100 FROM c WHERE a < 120) SELECT a FROM c",
    "INSERT INTO log (m) VALUES ('q') RETURNING m, m || m, length(m)",
    "INSERT INTO log (m) SELECT y FROM s RETURNING m",
    "INSERT INTO log (m) SELECT x FROM s UNION SELECT NULL RETURNING m",
    "INSERT INTO u (uq, ck) VALUES ('new', 1) RETURNING d",
    "INSERT OR IGNORE INTO u (uq, ck) VALUES ('fresh', 1) RETURNING k",
    "INSERT OR REPLACE INTO u (k, uq, ck) VALUES (1, 'z', 9) RETURNING k, uq, d, ck",
    "INSERT INTO u (k, uq, ck) VALUES (1, 'z', 9) ON CONFLICT (k) DO UPDATE SET uq = NULL RETURNING uq, d",
    "INSERT INTO u (uq, ck) VALUES ('a', 1) ON CONFLICT (uq) DO NOTHING RETURNING k",
    "INSERT INTO s (x, r, n) VALUES ('a', 1, NULL) RETURNING x, r, n, r + 1, n + 1",
    "INSERT INTO s (x, y, r) SELECT x, y, r FROM s RETURNING id, y",
    "UPDATE u SET uq = NULL WHERE k = 1 RETURNING uq, ck",
    "UPDATE u SET ck = ck + 1 RETURNING ck, d",
    "UPDATE u SET d = d + 1, uq = uq || 'x' RETURNING d, uq",
    "UPDATE s SET y = x WHERE id = 1 RETURNING y",
    "UPDATE s SET n = NULL RETURNING n, id",
    "UPDATE big SET b = a WHERE a > 1 RETURNING a, b, c",
    "DELETE FROM u WHERE k > 1 RETURNING k, uq, d, ck",
    "DELETE FROM u RETURNING uq",
    "DELETE FROM s WHERE n IS NULL RETURNING id, n, y",
    "DELETE FROM big WHERE a > 100 RETURNING a",
    "DELETE FROM w RETURNING k, v",
    "SELECT a FROM t WHERE b IN (SELECT y FROM s) UNION SELECT NULL",
    "SELECT (SELECT count(*) FROM s WHERE s.id = t.id) + (SELECT count(*) FROM t) FROM t",
    "SELECT (SELECT max(id) FROM s WHERE id > (SELECT min(id) FROM s)) FROM t",
    "SELECT a, (SELECT n FROM s ORDER BY n DESC LIMIT 1) FROM t",
    "WITH c AS (SELECT a FROM t), d AS (SELECT a FROM c UNION ALL SELECT a FROM c) SELECT a FROM d",
    "SELECT x FROM (SELECT x FROM s UNION ALL SELECT y FROM s) WHERE x IS NOT NULL",
    "SELECT * FROM (SELECT DISTINCT n FROM s) a JOIN (SELECT DISTINCT n FROM s) b ON a.n = b.n",
    "SELECT n FROM s WHERE id IN (SELECT id FROM s WHERE n IS NOT NULL)",
    "SELECT group_concat(x) FROM (SELECT x FROM s ORDER BY x LIMIT 2)",
    "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM s WHERE s.n = t.a) UNION ALL SELECT n FROM s",
    "SELECT t.a, s2.n FROM t, (SELECT n FROM s WHERE n IS NOT NULL) s2",
    "SELECT a FROM t EXCEPT SELECT a FROM t WHERE b IS NULL",
    "SELECT b FROM t UNION SELECT b FROM t WHERE 0",
    "SELECT n, (SELECT y FROM s s2 WHERE s2.id = s.id) FROM s",
    "SELECT * FROM (SELECT n FROM s UNION SELECT NULL) WHERE n IS NULL",
    "SELECT q.n FROM (SELECT n FROM s LIMIT 1 OFFSET 5) q",
    "SELECT coalesce(q.n, -1) FROM s LEFT JOIN (SELECT n, id FROM s WHERE id > 1 LIMIT 1) q ON q.id = s.id",
    "SELECT count(*) FROM (SELECT 1 UNION ALL SELECT NULL)",
    "SELECT * FROM (SELECT 1 AS a, NULL AS b UNION ALL SELECT NULL, 2)",
    "WITH RECURSIVE c(n) AS (SELECT n FROM s UNION SELECT n + 1 FROM c WHERE n < 8) SELECT n FROM c",
    "WITH RECURSIVE c(x) AS (SELECT x FROM s UNION ALL SELECT x || 'z' FROM c WHERE length(x) < 3) SELECT x FROM c",
    "INSERT INTO log (m) SELECT x FROM s WHERE n IS NULL RETURNING m, rowid",
    "UPDATE t SET b = (SELECT max(x) FROM s) WHERE id = 1 RETURNING b",
    "UPDATE u SET ck = (SELECT count(*) FROM s) RETURNING ck",
    "DELETE FROM u WHERE k IN (SELECT k FROM u WHERE uq IS NULL) RETURNING uq",
];

// probes that legitimately return no row on the populated database
const NO_ROWS: &[&str] = &[
    // the key conflicts, so DO NOTHING returns no row
    "INSERT INTO u (uq, ck) VALUES ('a', 1) ON CONFLICT (uq) DO NOTHING RETURNING k",
    // nothing in `big` is above 100
    "DELETE FROM big WHERE a > 100 RETURNING a",
    // OFFSET 5 is past the three rows of `s`
    "SELECT q.n FROM (SELECT n FROM s LIMIT 1 OFFSET 5) q",
];

#[test]
fn nothing_the_analysis_proves_for_a_probe_is_contradicted_by_running_it() -> TestResult {
    let empty = database()?;
    let full = database()?;
    full.execute_batch(ROWS)?;
    let mut broken = Vec::new();
    let mut proven = 0;
    let mut silent_rows = Vec::new();
    for sql in PROBES {
        let Ok(facts) = analyze(&full, sql, &|| false) else {
            continue;
        };
        let want: Vec<Col> = facts
            .iter()
            .map(|fact| (fact.class, fact.non_null, fact.pass_through))
            .collect();
        proven += want.iter().filter(|(_, non_null, _)| *non_null).count();
        broken.extend(rows_break_the_proof(&empty, sql, &want)?.0);
        let (found, seen) = rows_break_the_proof(&full, sql, &want)?;
        broken.extend(found);
        // a probe that returns nothing (or fails) on the populated database checks nothing
        if seen == 0 && !NO_ROWS.contains(sql) {
            silent_rows.push(*sql);
        }
    }
    assert!(silent_rows.is_empty(), "{silent_rows:#?}");
    assert!(proven > 30, "{proven}");
    assert!(broken.is_empty(), "{}", broken.join("\n"));
    Ok(())
}

#[test]
fn a_silent_statement_names_the_opcode_it_does_not_model() -> TestResult {
    let conn = database()?;
    for (sql, opcode) in [
        ("SELECT value FROM json_each('[1]')", "VOpen"),
        ("SELECT count(*) OVER () FROM t", "AggValue"),
        (
            "SELECT sum(a) OVER (ORDER BY id ROWS 1 PRECEDING) FROM t",
            "AggValue",
        ),
        (
            "SELECT dense_rank() OVER (PARTITION BY b ORDER BY a) FROM t",
            "SequenceTest",
        ),
        ("INSERT INTO fk (tid) VALUES (1) RETURNING id", "FkCounter"),
        ("UPDATE fk SET tid = 2 RETURNING tid", "FkIfZero"),
        (
            "SELECT a FROM t UNION ALL SELECT id FROM s ORDER BY 1 LIMIT 2",
            "Sort",
        ),
    ] {
        assert_eq!(
            analyze(&conn, sql, &|| false),
            Err(Silent::Unmodelled(opcode.to_owned())),
            "{sql}"
        );
    }
    // a statement that fires a trigger or checks a foreign key lists its sub-program after the program, which
    // restarts the numbering
    for sql in [
        "INSERT INTO t (a) VALUES (1) RETURNING id",
        "DELETE FROM t WHERE id = 1 RETURNING a",
    ] {
        assert_eq!(
            analyze(&conn, sql, &|| false),
            Err(Silent::Listing),
            "{sql}"
        );
    }
    assert_eq!(
        analyze(&conn, "SELECT a FROM t WHERE 0", &|| false),
        Err(Silent::NoResult)
    );
    assert!(matches!(
        analyze(&conn, "SELECT nope FROM t", &|| false),
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
fn an_origin_is_the_same_only_for_the_same_column_of_the_same_table() -> TestResult {
    let conn = database()?;
    conn.execute_batch(
        "ATTACH ':memory:' AS other;
         CREATE TABLE other.t (id INTEGER PRIMARY KEY, a INTEGER NOT NULL);
         CREATE TEMP TABLE t (id INTEGER PRIMARY KEY, a INTEGER NOT NULL);
         CREATE TABLE r (id INTEGER PRIMARY KEY, a INTEGER NOT NULL);
         CREATE INDEX r_a ON r (a);",
    )?;
    let proven = |sql: &str, pass: bool| {
        assert_eq!(
            columns(&conn, sql)
                .map(|cols| cols.iter().map(|col| (col.1, col.2)).collect::<Vec<_>>()),
            Ok(vec![(true, pass)]),
            "{sql}"
        );
    };
    // the same table name and column in two databases are two origins
    proven(
        "SELECT main.t.a FROM main.t UNION ALL SELECT other.t.a FROM other.t",
        false,
    );
    proven(
        "SELECT main.t.a FROM main.t UNION ALL SELECT temp.t.a FROM temp.t",
        false,
    );
    proven(
        "SELECT other.t.a FROM other.t UNION ALL SELECT other.t.a FROM other.t",
        true,
    );
    // the rowid and its alias are one origin, the rowid and another column are not
    proven("SELECT rowid FROM r UNION ALL SELECT id FROM r", true);
    proven("SELECT rowid FROM r UNION ALL SELECT a FROM r", false);
    // a column read through a covering index is the table's column
    proven(
        "SELECT a FROM r NOT INDEXED UNION ALL SELECT a FROM r INDEXED BY r_a",
        true,
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
    // the opcodes that keep triggers, virtual tables, windows and foreign keys silent
    for name in [
        "Program",
        "VOpen",
        "VColumn",
        "AggValue",
        "AggInverse",
        "FkCounter",
        "FkIfZero",
    ] {
        assert_eq!(Op::parse(name), None, "{name}");
    }
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
                analyze(&conn, case.sql, &|| false).is_err(),
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

// A floating-point NaN is stored as NULL, so only the arithmetic that cannot produce one may be non-null.
#[test]
fn a_nan_result_is_null_in_sqlite() -> TestResult {
    let conn = Connection::open_in_memory()?;
    conn.execute_batch(
        "CREATE TABLE f (p REAL NOT NULL, q REAL NOT NULL, z INTEGER NOT NULL);
         INSERT INTO f VALUES (9e999, -9e999, 0), (9e999, 9e999, 0);
         CREATE TABLE g (v REAL NOT NULL);
         INSERT INTO g VALUES (9e999), (-9e999);",
    )?;
    for (expression, expected) in [
        // computed from stored values, so SQLite cannot fold them
        ("(SELECT p + q FROM f LIMIT 1)", "null"),
        ("(SELECT p - p FROM f LIMIT 1)", "null"),
        ("(SELECT p * z FROM f LIMIT 1)", "null"),
        ("(SELECT p + z FROM f LIMIT 1)", "real"),
        ("(SELECT p - z FROM f LIMIT 1)", "real"),
        ("(SELECT total(v) FROM g)", "null"),
        ("(SELECT abs(p) FROM f LIMIT 1)", "real"),
        ("(SELECT round(p) FROM f LIMIT 1)", "real"),
        ("(SELECT round(p, 2) FROM f LIMIT 1)", "real"),
        ("(SELECT CAST(p AS REAL) FROM f LIMIT 1)", "real"),
        ("(SELECT CAST(p AS NUMERIC) FROM f LIMIT 1)", "real"),
        ("(SELECT sum(v) FROM g WHERE v > 0)", "real"),
        ("(SELECT avg(v) FROM g WHERE v > 0)", "real"),
        ("9e999 + -9e999", "null"),
        ("9e999 - 9e999", "null"),
        ("0 * 9e999", "null"),
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
            "total and avg are nullable reals",
            "SELECT total(a) AS t, avg(a) AS m FROM t",
            with(Construct::Aggregate, &["t"]),
            vec![
                (REAL_KIND, "REAL", true, "Unproven", Some("aggregate")),
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
    vec![(
        "the left of a left join keeps its column proof, the right is nullable",
        "SELECT t.a, s.x FROM t LEFT JOIN s ON t.id = s.id",
        with(Construct::OuterJoin, &["t", "s"]),
        vec![
            (INT_KIND, "INTEGER", false, "NotNullColumn", None),
            (TEXT_KIND, "TEXT", true, "Unproven", Some("outer join")),
        ],
    )]
}

fn origin_describe_cases() -> Vec<DescribeCase> {
    vec![
        (
            "a case choosing between two NOT NULL columns is computed",
            "SELECT CASE WHEN a > 1 THEN a ELSE id END AS v FROM t",
            from(&["t"]),
            vec![(None, "", false, "ComputedExpr", None)],
        ),
        (
            "a union all of two different NOT NULL columns is computed",
            "SELECT a FROM t UNION ALL SELECT a FROM big",
            with(Construct::CompoundSelect, &["t", "big"]),
            vec![(INT_KIND, "INTEGER", false, "ComputedExpr", None)],
        ),
        (
            "a unary plus keeps the read but SQLite names no origin, so it is computed",
            "SELECT +t.a, t.a FROM t LEFT JOIN s ON 1",
            with(Construct::OuterJoin, &["t", "s"]),
            vec![
                (None, "", false, "ComputedExpr", None),
                (INT_KIND, "INTEGER", false, "NotNullColumn", None),
            ],
        ),
        (
            "a union all of the same column stays a column",
            "SELECT a FROM t UNION ALL SELECT a FROM t",
            with(Construct::CompoundSelect, &["t"]),
            vec![(INT_KIND, "INTEGER", false, "NotNullColumn", None)],
        ),
        (
            "the left of a left join on a constant stays a column",
            "SELECT t.a FROM t LEFT JOIN s ON 1",
            with(Construct::OuterJoin, &["t", "s"]),
            vec![(INT_KIND, "INTEGER", false, "NotNullColumn", None)],
        ),
    ]
}

fn view_describe_cases() -> Vec<DescribeCase> {
    vec![
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
            "a compound select that needs an ephemeral table is analysed",
            "SELECT a FROM t UNION SELECT a FROM t",
            with(Construct::CompoundSelect, &["t"]),
            vec![(INT_KIND, "INTEGER", false, "NotNullColumn", None)],
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

fn nested_describe_cases() -> Vec<DescribeCase> {
    vec![
        (
            "a union of two NOT NULL columns of different tables is computed",
            "SELECT a FROM t UNION SELECT a FROM big",
            with(Construct::CompoundSelect, &["t", "big"]),
            vec![(INT_KIND, "INTEGER", false, "ComputedExpr", None)],
        ),
        (
            "a union with a nullable arm stays unproven",
            "SELECT a FROM t UNION SELECT n FROM s",
            with(Construct::CompoundSelect, &["t", "s"]),
            vec![(
                INT_KIND,
                "INTEGER",
                true,
                "Unproven",
                Some("compound select"),
            )],
        ),
        (
            "a union of literals is computed and typed",
            "SELECT 1 AS n UNION ALL SELECT 2",
            with(Construct::CompoundSelect, &[]),
            vec![(INT_KIND, "INTEGER", false, "ComputedExpr", None)],
        ),
        (
            "a scalar count is computed",
            "SELECT (SELECT count(*) FROM t) AS n",
            with(Construct::Subquery, &["t"]),
            vec![(INT_KIND, "INTEGER", false, "ComputedExpr", None)],
        ),
        (
            "a scalar max stays unproven",
            "SELECT (SELECT max(a) FROM t) AS m",
            with(Construct::Subquery, &["t"]),
            vec![(None, "", true, "Unproven", Some("subquery"))],
        ),
        (
            "exists is computed",
            "SELECT EXISTS (SELECT 1 FROM t) AS e",
            with(Construct::Subquery, &["t"]),
            vec![(INT_KIND, "INTEGER", false, "ComputedExpr", None)],
        ),
        (
            "a subquery that is not flattened keeps what it computes",
            "SELECT n FROM (SELECT count(*) AS n FROM t GROUP BY b LIMIT 5)",
            with(Construct::Subquery, &["t"]),
            vec![(INT_KIND, "INTEGER", false, "ComputedExpr", None)],
        ),
        (
            "a returning expression over an inserted literal is computed",
            "INSERT INTO log (m) VALUES ('x') RETURNING m || 'y' AS v",
            from(&["log"]),
            vec![(TEXT_KIND, "TEXT", false, "ComputedExpr", None)],
        ),
        (
            "a returning expression over a deleted nullable column stays unproven",
            "DELETE FROM log RETURNING m || 'y' AS v",
            from(&["log"]),
            vec![(TEXT_KIND, "TEXT", true, "Unproven", Some("expression"))],
        ),
        (
            "a returning coalesce of an updated column is computed",
            "UPDATE log SET m = 'y' RETURNING coalesce(m, 'z') AS v",
            from(&["log"]),
            vec![(None, "", false, "ComputedExpr", None)],
        ),
    ]
}

fn describe_mismatches(
    conn: &Connection,
    cases: impl IntoIterator<Item = DescribeCase>,
) -> Vec<String> {
    let mut wrong = Vec::new();
    for (name, sql, structure, want) in cases {
        let got = match describe(conn, sql, &structure, &|| false) {
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
    wrong
}

#[test]
fn describe_applies_the_bytecode_only_to_unproven_columns() -> TestResult {
    let conn = database()?;
    let wrong = describe_mismatches(
        &conn,
        expression_describe_cases()
            .into_iter()
            .chain(join_describe_cases())
            .chain(origin_describe_cases())
            .chain(view_describe_cases())
            .chain(nested_describe_cases()),
    );
    assert!(wrong.is_empty(), "{}", wrong.join("\n"));
    Ok(())
}

const COMPOUND_SCHEMA: &str = "
    CREATE TABLE cs (i INTEGER NOT NULL, tx TEXT NOT NULL, r REAL NOT NULL, i2 INTEGER NOT NULL) STRICT;
    CREATE TABLE ct (a INTEGER, b TEXT, d DATE NOT NULL, d2 DATE NOT NULL);
    CREATE VIEW csv AS SELECT i FROM cs UNION ALL SELECT tx FROM cs;
    CREATE VIEW csn AS SELECT i FROM cs UNION ALL SELECT NULL;
    CREATE VIEW ctv AS SELECT a, d FROM ct;
";

// SQLite declares a compound select's column with its leftmost arm, and a view, a subquery or a CTE with
// the column it selects, so that declaration alone says nothing about the other arms
fn compound_type_describe_cases() -> Vec<DescribeCase> {
    let compound = || with(Construct::CompoundSelect, &["cs"]);
    let nested = |construct| Structure {
        constructs: vec![construct, Construct::CompoundSelect],
        ..from(&["cs"])
    };
    let untyped_compound = (None, "", false, "ComputedExpr", None);
    vec![
        (
            "a union all of an integer and a text column is untyped",
            "SELECT i FROM cs UNION ALL SELECT tx FROM cs",
            compound(),
            vec![untyped_compound],
        ),
        (
            "the type of a later arm is not hidden by the first",
            "SELECT tx FROM cs UNION ALL SELECT i FROM cs",
            compound(),
            vec![untyped_compound],
        ),
        (
            "a union of an integer and a real column is untyped",
            "SELECT i FROM cs UNION SELECT r FROM cs",
            compound(),
            vec![untyped_compound],
        ),
        (
            "a compound inside a subquery is untyped",
            "SELECT k FROM (SELECT i AS k FROM cs UNION ALL SELECT tx FROM cs)",
            nested(Construct::Subquery),
            vec![untyped_compound],
        ),
        (
            "a compound inside a common table expression is untyped",
            "WITH c (k) AS (SELECT i FROM cs UNION ALL SELECT tx FROM cs) SELECT k FROM c",
            nested(Construct::CommonTableExpression),
            vec![untyped_compound],
        ),
        (
            "a compound inside a view is untyped",
            "SELECT i FROM csv",
            from(&["csv"]),
            vec![(None, "", false, "ComputedExpr", None)],
        ),
        (
            "a view whose compound has a NULL arm is nullable and keeps its type",
            "SELECT i FROM csn",
            from(&["csn"]),
            vec![(INT_KIND, "INTEGER", true, "Unproven", Some("view csn"))],
        ),
        (
            "a non-strict union of an integer and a text column is untyped",
            "SELECT a FROM ct UNION SELECT b FROM ct",
            with(Construct::CompoundSelect, &["ct"]),
            vec![(None, "", true, "Unproven", Some("compound select"))],
        ),
        (
            "a union all of two columns declared alike keeps the declaration",
            "SELECT i FROM cs UNION ALL SELECT i2 FROM cs",
            compound(),
            vec![(INT_KIND, "INTEGER", false, "ComputedExpr", None)],
        ),
        (
            "a non-strict union all of two columns declared alike keeps the declaration",
            "SELECT d FROM ct UNION ALL SELECT d2 FROM ct",
            with(Construct::CompoundSelect, &["ct"]),
            vec![(Some(Kind::Date), "DATE", false, "ComputedExpr", None)],
        ),
        (
            "a non-strict view over a plain column keeps its declaration",
            "SELECT a, d FROM ctv",
            from(&["ctv"]),
            vec![
                (INT_KIND, "INTEGER", true, "Unproven", Some("view ctv")),
                (Some(Kind::Date), "DATE", false, "NotNullColumn", None),
            ],
        ),
        (
            "a non-strict subquery over a plain column keeps its declaration",
            "SELECT d FROM (SELECT d FROM ct)",
            with(Construct::Subquery, &["ct"]),
            vec![(Some(Kind::Date), "DATE", false, "NotNullColumn", None)],
        ),
        (
            "an arm that is NULL keeps the declaration of the other",
            "SELECT d FROM ct UNION ALL SELECT NULL",
            with(Construct::CompoundSelect, &["ct"]),
            vec![(
                Some(Kind::Date),
                "DATE",
                true,
                "Unproven",
                Some("compound select"),
            )],
        ),
    ]
}

#[test]
fn a_compound_select_is_typed_by_every_arm_not_its_declaration() -> TestResult {
    let conn = database()?;
    conn.execute_batch(COMPOUND_SCHEMA)?;
    let wrong = describe_mismatches(&conn, compound_type_describe_cases());
    assert!(wrong.is_empty(), "{}", wrong.join("\n"));
    Ok(())
}

fn analyses_run(sql: &str) -> Result<usize, Box<dyn std::error::Error>> {
    let conn = database()?;
    let before = ANALYSES.with(Cell::get);
    describe(&conn, sql, &from(&["t"]), &|| false)?;
    Ok(ANALYSES.with(Cell::get) - before)
}

#[test]
fn the_bytecode_is_analysed_only_for_a_column_that_needs_it() -> TestResult {
    // proven by the existing rule and typed by its declaration
    assert_eq!(analyses_run("SELECT id, a, b FROM t")?, 0);
    // an override decides the nullability and the declaration the type
    assert_eq!(analyses_run("SELECT a AS \"x!\", b AS \"y?\" FROM t")?, 0);
    // a parameter-only statement has no column
    assert_eq!(analyses_run("INSERT INTO log (m) VALUES ('x')")?, 0);
    // an unproven expression needs it, once however many columns do
    assert_eq!(analyses_run("SELECT a + 1, a * 2 FROM t")?, 1);
    // so does an untyped column the existing rule proves, for its class
    assert_eq!(analyses_run("SELECT c FROM t")?, 1);
    Ok(())
}

#[test]
fn describing_an_explain_statement_never_reads_its_bytecode() -> TestResult {
    let conn = database()?;
    let described = describe(&conn, "EXPLAIN SELECT 1", &Structure::default(), &|| false)?;
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
        describe(&conn, sql, &Structure::default(), &|| false)?;
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
    let machine = super::Machine::new(&program, layouts);
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

// a straight-line program of more steps than one stop interval
fn wide_select() -> String {
    let columns: Vec<String> = (1..=700).map(|n| format!("a + {n}")).collect();
    format!("SELECT {} FROM t", columns.join(", "))
}

#[test]
fn a_stop_check_that_fires_stops_the_analysis_with_the_stop_outcome() -> TestResult {
    let conn = database()?;
    let sql = wide_select();
    assert_eq!(analyze(&conn, &sql, &|| true), Err(Silent::Stopped));
    Ok(())
}

#[test]
fn a_stop_check_that_never_fires_leaves_the_analysis_unchanged() -> TestResult {
    let conn = database()?;
    let sql = wide_select();
    let calls = Cell::new(0_usize);
    let counting = || {
        calls.set(calls.get() + 1);
        false
    };
    let counted = analyze(&conn, &sql, &counting)?;
    assert_eq!(counted, analyze(&conn, &sql, &|| false)?);
    assert_eq!(counted.len(), 700);
    // consulted periodically, not on every step
    assert!(calls.get() > 0);
    assert!(calls.get() < 1_000);
    Ok(())
}

#[test]
fn a_stopped_analysis_fails_the_describe_as_an_interrupt() -> TestResult {
    let conn = database()?;
    let sql = wide_select();
    let err = describe(&conn, &sql, &from(&["t"]), &|| true);
    assert_eq!(err, Err(crate::exec::interrupted()));
    Ok(())
}

// programs written by hand, for what the compiler never emits
fn program(ops: &[(&str, i64, i64, i64)]) -> Vec<super::Insn> {
    ops.iter()
        .map(|(name, p1, p2, p3)| super::Insn {
            op: Op::parse(name),
            name: (*name).to_owned(),
            p1: *p1,
            p2: *p2,
            p3: *p3,
            p4: String::new(),
            p5: 0,
        })
        .collect()
}

fn facts_of(ops: &[(&str, i64, i64, i64)]) -> Result<Vec<(Option<Class>, bool)>, Silent> {
    let program = program(ops);
    let machine = super::Machine::new(&program, std::collections::HashMap::new());
    Ok(super::run(&machine, &|| false)?
        .iter()
        .map(|fact| (fact.class, fact.non_null))
        .collect())
}

#[test]
fn a_once_jump_carries_what_the_first_run_left() {
    // the register is NULL until the body runs, and the jump only happens after it did
    assert_eq!(
        facts_of(&[
            ("Init", 0, 1, 0),
            ("Null", 0, 1, 0),
            ("Once", 0, 4, 0),
            ("Integer", 5, 1, 0),
            ("ResultRow", 1, 1, 0),
            ("Goto", 0, 2, 0),
        ]),
        Ok(vec![(Some(Class::Integer), true)])
    );
}

#[test]
fn an_ephemeral_table_read_sees_a_write_later_in_the_program() {
    // The read loops over the rows, and a NULL is written after the first read. Through a second cursor of the
    // same table too.
    for writer in [0, 1] {
        assert_eq!(
            facts_of(&[
                ("Init", 0, 1, 0),
                ("OpenEphemeral", 0, 1, 0),
                ("OpenDup", 1, 0, 0),
                ("Integer", 1, 1, 0),
                ("MakeRecord", 1, 1, 2),
                ("NewRowid", 0, 3, 0),
                ("Insert", 0, 2, 3),
                ("Rewind", 0, 15, 0),
                ("Column", 0, 0, 4),
                ("ResultRow", 4, 1, 0),
                ("Null", 0, 1, 0),
                ("MakeRecord", 1, 1, 2),
                ("NewRowid", writer, 3, 0),
                ("Insert", writer, 2, 3),
                ("Next", 0, 8, 0),
                ("Halt", 0, 0, 0),
            ]),
            Ok(vec![(Some(Class::Integer), false)]),
            "writer cursor {writer}"
        );
    }
}

#[test]
fn an_ephemeral_table_of_integers_stays_non_null_when_nothing_else_is_written() {
    assert_eq!(
        facts_of(&[
            ("Init", 0, 1, 0),
            ("OpenEphemeral", 0, 1, 0),
            ("OpenDup", 1, 0, 0),
            ("Integer", 1, 1, 0),
            ("MakeRecord", 1, 1, 2),
            ("NewRowid", 1, 3, 0),
            ("Insert", 1, 2, 3),
            ("Rewind", 0, 11, 0),
            ("Column", 0, 0, 4),
            ("ResultRow", 4, 1, 0),
            ("Next", 0, 8, 0),
            ("Halt", 0, 0, 0),
        ]),
        Ok(vec![(Some(Class::Integer), true)])
    );
}

#[test]
fn a_cursor_on_a_row_of_a_table_nothing_was_written_to_is_never_reached() {
    // Rewind found a row, which an empty table cannot give
    assert_eq!(
        facts_of(&[
            ("Init", 0, 1, 0),
            ("OpenEphemeral", 0, 1, 0),
            ("Rewind", 0, 5, 0),
            ("Column", 0, 0, 1),
            ("ResultRow", 1, 1, 0),
            ("Halt", 0, 0, 0),
        ]),
        Err(Silent::NoResult)
    );
    // a cursor that was never moved is on a null row, whatever the table holds
    assert_eq!(
        facts_of(&[
            ("Init", 0, 1, 0),
            ("OpenEphemeral", 0, 1, 0),
            ("Column", 0, 0, 1),
            ("ResultRow", 1, 1, 0),
            ("Halt", 0, 0, 0),
        ]),
        Ok(vec![(None, false)])
    );
}

#[test]
fn a_write_through_a_cursor_that_may_be_any_table_is_silent() {
    // never opened
    assert_eq!(
        facts_of(&[
            ("Init", 0, 1, 0),
            ("Integer", 1, 1, 0),
            ("MakeRecord", 1, 1, 2),
            ("Insert", 5, 2, 3),
            ("ResultRow", 1, 1, 0),
            ("Halt", 0, 0, 0),
        ]),
        Err(Silent::Cursor)
    );
    // the same cursor number is a table on one path and a pseudo cursor on the other
    assert_eq!(
        facts_of(&[
            ("Init", 0, 1, 0),
            ("Integer", 1, 1, 0),
            ("MakeRecord", 1, 1, 2),
            ("Integer", 1, 5, 0),
            ("If", 5, 8, 0),
            ("OpenEphemeral", 0, 1, 0),
            ("Goto", 0, 9, 0),
            ("Noop", 0, 0, 0),
            ("OpenPseudo", 0, 2, 1),
            ("Insert", 0, 2, 3),
            ("ResultRow", 1, 1, 0),
            ("Halt", 0, 0, 0),
        ]),
        Err(Silent::Cursor)
    );
}

#[test]
fn a_coroutine_register_needs_a_known_address() {
    // never set up
    assert_eq!(
        facts_of(&[("Init", 0, 1, 0), ("Yield", 1, 0, 0), ("Halt", 0, 0, 0),]),
        Err(Silent::ReturnAddress)
    );
    // set to a number
    assert_eq!(
        facts_of(&[
            ("Init", 0, 1, 0),
            ("Integer", 3, 1, 0),
            ("Yield", 1, 0, 0),
            ("Halt", 0, 0, 0),
        ]),
        Err(Silent::ReturnAddress)
    );
    // EndCoroutine returns to the address after a Yield, and this one is after nothing
    assert_eq!(
        facts_of(&[
            ("Init", 0, 1, 0),
            ("InitCoroutine", 1, 0, 3),
            ("EndCoroutine", 1, 0, 0),
            ("Halt", 0, 0, 0),
        ]),
        Err(Silent::ReturnAddress)
    );
    // a Yield that has no jump of its own to go to when the coroutine ends
    assert_eq!(
        facts_of(&[
            ("Init", 0, 1, 0),
            ("InitCoroutine", 1, 0, 4),
            ("Yield", 1, 0, 0),
            ("Halt", 0, 0, 0),
            ("EndCoroutine", 1, 0, 0),
        ]),
        Err(Silent::ReturnAddress)
    );
}

#[test]
fn a_coroutine_hands_its_registers_to_the_caller() {
    // the coroutine yields 7 and then NULL, and ends; the caller reads the register after each Yield
    assert_eq!(
        facts_of(&[
            ("Init", 0, 1, 0),
            ("InitCoroutine", 1, 7, 2),
            ("Integer", 7, 2, 0),
            ("Yield", 1, 0, 0),
            ("Null", 0, 2, 0),
            ("Yield", 1, 0, 0),
            ("EndCoroutine", 1, 0, 0),
            ("Yield", 1, 10, 0),
            ("ResultRow", 2, 1, 0),
            ("Goto", 0, 7, 0),
            ("Halt", 0, 0, 0),
        ]),
        Ok(vec![(Some(Class::Integer), false)])
    );
    // with only the first, the register is never NULL when it is read
    assert_eq!(
        facts_of(&[
            ("Init", 0, 1, 0),
            ("InitCoroutine", 1, 5, 2),
            ("Integer", 7, 2, 0),
            ("Yield", 1, 0, 0),
            ("EndCoroutine", 1, 0, 0),
            ("Yield", 1, 8, 0),
            ("ResultRow", 2, 1, 0),
            ("Goto", 0, 5, 0),
            ("Halt", 0, 0, 0),
        ]),
        Ok(vec![(Some(Class::Integer), true)])
    );
}

#[test]
fn a_return_of_a_subroutine_that_may_be_entered_in_line() {
    // in line, the register is NULL and the Return falls through
    assert_eq!(
        facts_of(&[
            ("Init", 0, 1, 0),
            ("BeginSubrtn", 0, 2, 0),
            ("Integer", 7, 3, 0),
            ("Return", 2, 0, 1),
            ("ResultRow", 3, 1, 0),
            ("Halt", 0, 0, 0),
        ]),
        Ok(vec![(Some(Class::Integer), true)])
    );
    // called, it goes back to the caller, and falls through when it is not an address
    assert_eq!(
        facts_of(&[
            ("Init", 0, 1, 0),
            ("Gosub", 2, 4, 0),
            ("ResultRow", 3, 1, 0),
            ("Halt", 0, 0, 0),
            ("Integer", 7, 3, 0),
            ("Return", 2, 0, 1),
        ]),
        Ok(vec![(Some(Class::Integer), true)])
    );
    // a register that holds a number that is not an address has nowhere to go
    assert_eq!(
        facts_of(&[
            ("Init", 0, 1, 0),
            ("Integer", 4, 2, 0),
            ("Return", 2, 0, 1),
            ("ResultRow", 2, 1, 0),
            ("Halt", 0, 0, 0),
        ]),
        Err(Silent::ReturnAddress)
    );
}

#[test]
fn halt_if_null_only_lets_a_value_that_is_not_null_go_on() {
    assert_eq!(
        facts_of(&[
            ("Init", 0, 1, 0),
            ("Variable", 1, 1, 0),
            ("HaltIfNull", 1299, 2, 1),
            ("ResultRow", 1, 1, 0),
            ("Halt", 0, 0, 0),
        ]),
        Ok(vec![(None, true)])
    );
    assert_eq!(
        facts_of(&[
            ("Init", 0, 1, 0),
            ("Null", 0, 1, 0),
            ("HaltIfNull", 1299, 2, 1),
            ("ResultRow", 1, 1, 0),
            ("Halt", 0, 0, 0),
        ]),
        Err(Silent::NoResult)
    );
}

#[test]
fn a_zero_target_removes_the_jump_only_where_sqlite_never_takes_it() -> TestResult {
    let state = super::State::default();
    for (name, target, edges) in [
        ("Rewind", 0, 1),
        ("Rewind", 2, 2),
        ("Last", 0, 1),
        ("Last", 2, 2),
        ("SeekRowid", 0, 1),
        ("NotExists", 0, 1),
        ("SeekRowid", 2, 2),
        // the rows above are the opcodes whose zero target is no jump (an assertion or an error in SQLite); SeekGE
        // is the control, which keeps both edges whatever its target
        ("SeekGE", 0, 2),
    ] {
        let program = program(&[(name, 0, target, 0), ("Halt", 0, 0, 0), ("Halt", 0, 0, 0)]);
        let machine = super::Machine::new(&program, std::collections::HashMap::new());
        assert_eq!(
            machine.step(0, &state)?.len(),
            edges,
            "{name} with target {target}"
        );
    }
    Ok(())
}

#[test]
fn a_rewind_with_no_target_finds_a_row_and_a_last_with_none_may_not() -> TestResult {
    let state = super::State::default();
    let both = super::ROW | super::NULL_ROW;
    for (name, rows) in [("Rewind", super::ROW), ("Last", both)] {
        let program = program(&[(name, 0, 0, 0), ("Halt", 0, 0, 0)]);
        let machine = super::Machine::new(&program, std::collections::HashMap::new());
        let edges = machine.step(0, &state)?;
        let [(addr, edge)] = edges.as_slice() else {
            return Err(format!("{name} has {} edges", edges.len()).into());
        };
        assert_eq!(*addr, 1);
        assert_eq!(
            edge.cursors.get(&0).map(|cursor| cursor.rows),
            Some(rows),
            "{name}"
        );
    }
    Ok(())
}

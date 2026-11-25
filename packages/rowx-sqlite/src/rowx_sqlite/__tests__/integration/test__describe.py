from typing import TYPE_CHECKING, Final

import pytest
import pytest_asyncio
from rowx_core.errors import DatabaseError
from rowx_sqlite import ConnectOptions, connect
from rowx_sqlite._core import connect_checker, describe as describe_impl
from rowx_sqlite._describe import describe

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from rowx_sqlite._core import Connection

SCHEMA: Final[tuple[str, ...]] = (
    """CREATE TABLE t (
        id INTEGER PRIMARY KEY,
        a INTEGER NOT NULL,
        b TEXT,
        c,
        g INTEGER GENERATED ALWAYS AS (a + 1) VIRTUAL,
        h INTEGER NOT NULL GENERATED ALWAYS AS (a + 1) STORED
    )""",
    "CREATE TABLE s (id INTEGER PRIMARY KEY, x TEXT NOT NULL) STRICT",
    "CREATE TABLE r (a TEXT NOT NULL, b TEXT)",
    "CREATE TABLE typed (i INTEGER, f REAL, tx TEXT, bl BLOB, bo BOOLEAN, dt DATETIME, u UUID, j JSON, n DECIMAL(10, 2))",
    "CREATE TABLE w (k INTEGER PRIMARY KEY, v TEXT) WITHOUT ROWID",
    "CREATE TABLE pk2 (id INTEGER, v TEXT, PRIMARY KEY (id))",
    "CREATE VIEW v AS SELECT a, b FROM t",
    "CREATE VIEW vl AS SELECT t.a, s.x FROM t LEFT JOIN s ON t.id = s.id",
    "CREATE VIEW unsafe AS SELECT a, rtreedepth(zeroblob(8)) AS d FROM t",
)

type Expected = tuple[tuple[str, bool, str], ...]

NN: Final = "NotNullColumn"
NULLABLE: Final = "NullableColumn"
UNPROVEN: Final = "Unproven"
COMPUTED: Final = "ComputedExpr"


@pytest_asyncio.fixture
async def dsn(tmp_path: Path) -> str:
    path = tmp_path / "check.db"
    writer = await connect(ConnectOptions(path=str(path), mode="rwc"))
    try:
        for statement in SCHEMA:
            await writer._impl._execute(statement, [])
    finally:
        await writer.close()
    return f"sqlite:///{path}"


@pytest_asyncio.fixture
async def checker(dsn: str) -> AsyncIterator[Connection]:
    conn = await connect_checker(dsn)
    try:
        yield conn
    finally:
        await conn.close()


CASES: Final[dict[str, tuple[str, Expected]]] = {
    "strict table direct columns": ("SELECT id, x FROM s", (("id", False, NN), ("x", False, NN))),
    "ordinary table direct columns": (
        "SELECT a, b, c FROM t",
        (("a", False, NN), ("b", True, NULLABLE), ("c", True, NULLABLE)),
    ),
    "aliases": (
        "SELECT a AS first, b AS second, t.a AS third FROM t",
        (("first", False, NN), ("second", True, NULLABLE), ("third", False, NN)),
    ),
    "view": ("SELECT a, b FROM v", (("a", False, NN), ("b", True, UNPROVEN))),
    "view over a left join": ("SELECT a, x FROM vl", (("a", False, NN), ("x", True, UNPROVEN))),
    "cte": ("WITH c AS (SELECT a FROM t) SELECT a FROM c", (("a", False, NN),)),
    "cte shadowing a table name": ("WITH s AS (SELECT a AS x FROM t) SELECT x FROM s", (("x", False, NN),)),
    "scalar subquery": (
        "SELECT a, (SELECT max(x) FROM s) AS m FROM t",
        (("a", True, UNPROVEN), ("m", True, UNPROVEN)),
    ),
    "from subquery": ("SELECT a FROM (SELECT a FROM t)", (("a", False, NN),)),
    "union all with a null arm": ("SELECT a FROM t UNION ALL SELECT NULL", (("a", True, UNPROVEN),)),
    "inner join": (
        "SELECT t.a, s.x FROM t INNER JOIN s ON t.id = s.id",
        (("a", False, NN), ("x", False, NN)),
    ),
    "left join": ("SELECT t.a, s.x FROM t LEFT JOIN s ON t.id = s.id", (("a", False, NN), ("x", True, UNPROVEN))),
    "right join": (
        "SELECT t.a, s.x FROM t RIGHT JOIN s ON t.id = s.id",
        (("a", True, UNPROVEN), ("x", True, UNPROVEN)),
    ),
    "full join": (
        "SELECT t.a, s.x FROM t FULL JOIN s ON t.id = s.id",
        (("a", True, UNPROVEN), ("x", True, UNPROVEN)),
    ),
    "natural join": ("SELECT a, x FROM t NATURAL JOIN s", (("a", False, NN), ("x", False, NN))),
    "inner join using": ("SELECT t.a, s.x FROM t JOIN s USING (id)", (("a", False, NN), ("x", False, NN))),
    "left join using": ("SELECT t.a, s.x FROM t LEFT JOIN s USING (id)", (("a", False, NN), ("x", True, UNPROVEN))),
    "comma join": ("SELECT t.a, s.x FROM t, s", (("a", False, NN), ("x", False, NN))),
    "cross join": ("SELECT t.a, s.x FROM t CROSS JOIN s", (("a", False, NN), ("x", False, NN))),
    "aggregates": (
        "SELECT count(*) AS n, max(a) AS m, a + 1 AS p FROM t",
        (("n", False, "ComputedExpr"), ("m", True, UNPROVEN), ("p", True, UNPROVEN)),
    ),
    "aggregate without group by": (
        "SELECT a, max(a) AS m FROM t",
        (("a", True, UNPROVEN), ("m", True, UNPROVEN)),
    ),
    "count without group by": (
        "SELECT id, count(*) AS n FROM t",
        (("id", True, UNPROVEN), ("n", False, "ComputedExpr")),
    ),
    "quoted count without group by": (
        'SELECT a, "count"(*) AS n FROM t',
        (("a", True, UNPROVEN), ("n", False, "ComputedExpr")),
    ),
    "min without group by": ("SELECT id, min(a) AS m FROM t", (("id", True, UNPROVEN), ("m", True, UNPROVEN))),
    "having without group by": (
        "SELECT a, count(*) AS n FROM t HAVING n >= 0",
        (("a", True, UNPROVEN), ("n", False, "ComputedExpr")),
    ),
    "group by without aggregate": ("SELECT a FROM t GROUP BY a", (("a", False, NN),)),
    "aggregate with group by": (
        "SELECT a, count(*) AS n FROM t GROUP BY a",
        (("a", False, NN), ("n", False, "ComputedExpr")),
    ),
    "two argument max is scalar": ("SELECT max(a, 1) AS m, a FROM t", (("m", True, UNPROVEN), ("a", False, NN))),
    "window function is not an aggregate": (
        "SELECT a, count(*) OVER () AS n FROM t",
        (("a", False, NN), ("n", True, UNPROVEN)),
    ),
    "aggregate with its own order by terms": (
        "SELECT a, max(a ORDER BY a, a) AS m FROM t",
        (("a", True, UNPROVEN), ("m", True, UNPROVEN)),
    ),
    "over as a column alias": (
        "SELECT a, count(*) over FROM t",
        (("a", True, UNPROVEN), ("over", False, "ComputedExpr")),
    ),
    "without rowid primary key": ("SELECT k FROM w", (("k", False, NN),)),
    "table constraint rowid alias": ("SELECT id FROM pk2", (("id", False, NN),)),
    "insert select returning": (
        "INSERT INTO t (a) SELECT a FROM t RETURNING id, a",
        (("id", False, NN), ("a", False, NN)),
    ),
    "table valued function": ("SELECT value FROM json_each('[1]')", (("value", True, UNPROVEN),)),
    "insert returning": (
        "INSERT INTO t (a) VALUES (1) RETURNING id, a, b",
        (("id", False, NN), ("a", False, NN), ("b", True, NULLABLE)),
    ),
    "update returning": (
        "UPDATE t SET b = 'x' RETURNING id, a, b",
        (("id", False, NN), ("a", False, NN), ("b", True, NULLABLE)),
    ),
    "delete returning": (
        "DELETE FROM t WHERE a = 1 RETURNING a, b",
        (("a", False, NN), ("b", True, NULLABLE)),
    ),
    "insert without returning": ("INSERT INTO t (a) VALUES (1)", ()),
    "update without returning": ("UPDATE t SET b = 'x'", ()),
    "delete without returning": ("DELETE FROM t", ()),
    "generated columns": ("SELECT g, h FROM t", (("g", True, NULLABLE), ("h", False, NN))),
    "rowid alias": ("SELECT id, rowid, oid FROM t", (("id", False, NN), ("id", False, NN), ("id", False, NN))),
    "rowid alias under a left join": ("SELECT s.id FROM t LEFT JOIN s ON t.id = s.id", (("id", True, UNPROVEN),)),
    "rowid alias through a view": ("SELECT a FROM v", (("a", False, NN),)),
    "implicit rowid": ("SELECT rowid FROM r", (("rowid", False, NN),)),
    "count star": ("SELECT count(*) AS n FROM s", (("n", False, COMPUTED),)),
    "count star over an empty group": ("SELECT count(*) AS n FROM t WHERE 0", (("n", False, COMPUTED),)),
    "total is never null, sum is": (
        "SELECT total(a) AS t, sum(a) AS s FROM t",
        (("t", False, COMPUTED), ("s", True, UNPROVEN)),
    ),
    "avg max and min are not proven": (
        "SELECT avg(a) AS v, max(a) AS x, min(a) AS n FROM t",
        (("v", True, UNPROVEN), ("x", True, UNPROVEN), ("n", True, UNPROVEN)),
    ),
    "literals": (
        "SELECT 1 AS one, 'x' AS text, 1.5 AS real",
        (("one", False, COMPUTED), ("text", False, COMPUTED), ("real", False, COMPUTED)),
    ),
    "null literal": ("SELECT NULL AS nil", (("nil", True, UNPROVEN),)),
    "coalesce of a nullable column": (
        "SELECT coalesce(b, 'z') AS v, ifnull(b, 0) AS w FROM t",
        (("v", False, COMPUTED), ("w", False, COMPUTED)),
    ),
    "coalesce of two nullable columns": ("SELECT coalesce(b, c) AS v FROM t", (("v", True, UNPROVEN),)),
    "case with and without an else": (
        "SELECT CASE WHEN a > 1 THEN 'x' ELSE 'y' END AS p, CASE WHEN a > 1 THEN 'x' END AS q FROM t",
        (("p", False, COMPUTED), ("q", True, UNPROVEN)),
    ),
    "concatenation follows its operands": (
        "SELECT a || 'x' AS p, b || 'x' AS q FROM t",
        (("p", False, COMPUTED), ("q", True, UNPROVEN)),
    ),
    "arithmetic is non-null unless it divides": (
        "SELECT a + 1 AS p, a / 1 AS q, a % 2 AS r FROM t",
        (("p", False, COMPUTED), ("q", True, UNPROVEN), ("r", True, UNPROVEN)),
    ),
    "functions": (
        "SELECT length(a) AS p, length(b) AS q, typeof(b) AS r, json_array(a) AS s FROM t",
        (("p", False, COMPUTED), ("q", True, UNPROVEN), ("r", False, COMPUTED), ("s", True, UNPROVEN)),
    ),
    "cast": (
        "SELECT CAST(a AS TEXT) AS p, CAST(b AS TEXT) AS q FROM t",
        (("p", False, COMPUTED), ("q", True, UNPROVEN)),
    ),
    "the right side of a left join under coalesce": (
        "SELECT coalesce(s.x, 'z') AS p, s.x AS q FROM t LEFT JOIN s ON t.id = s.id",
        (("p", False, COMPUTED), ("q", True, UNPROVEN)),
    ),
    "a left join the where clause makes inner": (
        "SELECT s.x FROM t LEFT JOIN s ON t.id = s.id WHERE s.x IS NOT NULL",
        (("x", False, NN),),
    ),
    "a table valued function stays unproven": ("SELECT value FROM json_each('[1]')", (("value", True, UNPROVEN),)),
    "a recursive cte stays unproven": (
        "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 3) SELECT n FROM c",
        (("n", True, UNPROVEN),),
    ),
    "bang override": ('SELECT b AS "b!" FROM t', (("b", False, "OverrideBang"),)),
    "question override": ('SELECT a AS "a?" FROM t', (("a", True, "OverrideQuestion"),)),
    "bang override on an unproven column": ('SELECT count(*) AS "n!" FROM t', (("n", False, "OverrideBang"),)),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(("sql", "expected"), CASES.values(), ids=CASES.keys())
async def test_each_construct_has_its_nullability(checker: Connection, sql: str, expected: Expected) -> None:
    described = await describe(checker, sql)
    assert tuple((col.name, col.nullable, col.reason) for col in described.columns) == expected


@pytest.mark.asyncio
async def test_unproven_reasons_name_their_cause(checker: Connection) -> None:
    # nullable columns, so that the bytecode proves nothing the structure denies
    causes = {
        "SELECT b FROM v": "view v",
        "SELECT b FROM main.v": "view main.v",
        "WITH c AS (SELECT b FROM t) SELECT b FROM c": "common table expression",
        "SELECT b FROM (SELECT b FROM t)": "subquery",
        "SELECT b FROM t UNION SELECT b FROM t": "compound select",
        "SELECT s.x FROM t LEFT JOIN s ON 1": "outer join",
        "SELECT b || 'x' AS n FROM t": "expression",
        "SELECT b, max(a) AS m FROM t": "aggregate",
        "SELECT b, sum(a) AS n FROM t HAVING n >= 0": "aggregate",
        "SELECT name FROM sqlite_master": "source sqlite_master is not a table",
        "SELECT value FROM json_each('[1]')": "source json_each is not a table",
    }
    for sql, cause in causes.items():
        described = await describe(checker, sql)
        assert {col.reason_detail for col in described.columns} == {cause}, sql


@pytest.mark.asyncio
async def test_a_proven_column_has_no_cause(checker: Connection) -> None:
    for sql in (
        "SELECT a FROM v",
        "WITH c AS (SELECT a FROM t) SELECT a FROM c",
        "SELECT count(*) AS n FROM t",
        "SELECT t.a FROM t LEFT JOIN s ON 1",
        "SELECT coalesce(b, 'z') AS n FROM t",
    ):
        described = await describe(checker, sql)
        assert [(col.nullable, col.reason_detail) for col in described.columns] == [(False, None)], sql


@pytest.mark.asyncio
async def test_a_base_column_reports_its_origin_through_a_view(checker: Connection) -> None:
    (col,) = (await describe(checker, "SELECT a AS renamed FROM v")).columns
    assert (col.database, col.table, col.column) == ("main", "t", "a")
    (computed,) = (await describe(checker, "SELECT a + 1 FROM t")).columns
    assert (computed.database, computed.table, computed.column) == (None, None, None)


@pytest.mark.asyncio
async def test_overrides_keep_the_raw_name(checker: Connection) -> None:
    bang, question = (await describe(checker, 'SELECT b AS "b!", a AS "a?" FROM t')).columns
    assert (bang.name, bang.raw_name) == ("b", "b!")
    assert (question.name, question.raw_name) == ("a", "a?")


@pytest.mark.asyncio
async def test_parameters_have_a_count_names_and_no_type(checker: Connection) -> None:
    described = await describe(checker, "SELECT a FROM t WHERE a = ?1 AND b = :name AND c = ?")
    assert [param.name for param in described.params] == ["?1", ":name", None]
    assert all((param.type_key, param.type_name, param.element_key) == (None, "", None) for param in described.params)
    assert (await describe(checker, "SELECT a FROM t")).params == []


@pytest.mark.asyncio
async def test_type_keys_come_from_the_declared_type(checker: Connection) -> None:
    described = await describe(checker, "SELECT i, f, tx, bl, bo, dt, u, j, n FROM typed")
    assert [col.type_key for col in described.columns] == [
        "integer",
        "real",
        "text",
        "blob",
        "boolean",
        "datetime",
        "uuid",
        "json",
        "decimal",
    ]
    assert [col.type_name for col in described.columns][-1] == "DECIMAL(10, 2)"
    assert all(col.element_key is None for col in described.columns)


@pytest.mark.asyncio
async def test_a_column_without_a_declared_type_is_untyped(checker: Connection) -> None:
    c, expression = (await describe(checker, "SELECT c, a + 1 FROM t")).columns
    assert (c.type_key, c.type_name) == (None, "")
    assert expression.type_key is None


@pytest.mark.asyncio
async def test_describing_does_not_execute_the_statement(dsn: str, checker: Connection) -> None:
    writer = await connect(ConnectOptions(path=dsn.removeprefix("sqlite:///"), mode="rw"))
    try:
        await describe(checker, "DELETE FROM t")
        await describe(checker, "INSERT INTO t (a) VALUES (1) RETURNING id")
        rows = await writer._impl._fetch("SELECT count(*) FROM t", [], None, True, True, None, None)  # ruff: ignore[boolean-positional-value-in-call]
        assert rows == [0]
    finally:
        await writer.close()


@pytest.mark.asyncio
async def test_the_checker_connection_refuses_a_write(checker: Connection) -> None:
    with pytest.raises(DatabaseError) as raised:
        await checker._execute("INSERT INTO t (a) VALUES (1)", [])
    assert raised.value.sqlstate == "25006"
    assert raised.value.code is not None
    assert raised.value.code.startswith("SQLITE_READONLY")


@pytest.mark.asyncio
async def test_a_view_with_an_untrusted_function_fails_to_describe_under_the_checker(
    dsn: str, checker: Connection
) -> None:
    with pytest.raises(DatabaseError, match="unsafe use of rtreedepth"):
        await describe(checker, "SELECT a FROM unsafe")
    # the same view describes on a trusting connection
    trusting = await connect(ConnectOptions(path=dsn.removeprefix("sqlite:///"), mode="rw"))
    try:
        (col, _) = (await describe(trusting._impl, "SELECT a, d FROM unsafe")).columns
        assert col.name == "a"
    finally:
        await trusting.close()


@pytest.mark.asyncio
async def test_a_describe_sees_a_schema_another_connection_changed(dsn: str, checker: Connection) -> None:
    before = await describe(checker, "SELECT * FROM r")
    assert [col.name for col in before.columns] == ["a", "b"]
    writer = await connect(ConnectOptions(path=dsn.removeprefix("sqlite:///"), mode="rw"))
    try:
        await writer._impl._execute("ALTER TABLE r ADD COLUMN extra TEXT", [])
    finally:
        await writer.close()
    after = await describe(checker, "SELECT * FROM r")
    assert [col.name for col in after.columns] == ["a", "b", "extra"]


@pytest.mark.asyncio
@pytest.mark.parametrize("sql", ["PRAGMA trusted_schema=ON", "PRAGMA query_only=OFF", "PRAGMA trusted_schema(1)"])
async def test_describing_a_pragma_that_sets_a_value_is_refused_and_changes_nothing(
    checker: Connection, sql: str
) -> None:
    with pytest.raises(DatabaseError) as raised:
        await describe(checker, sql)
    assert raised.value.code == "SQLITE_AUTH"
    assert await checker._fetch("PRAGMA trusted_schema", [], None, True, True, None, None) == [0]  # ruff: ignore[boolean-positional-value-in-call]
    assert await checker._fetch("PRAGMA query_only", [], None, True, True, None, None) == [1]  # ruff: ignore[boolean-positional-value-in-call]
    with pytest.raises(DatabaseError, match="unsafe use of rtreedepth"):
        await describe(checker, "SELECT a FROM unsafe")


@pytest.mark.asyncio
@pytest.mark.parametrize("sql", ["PRAGMA table_info(t)", "PRAGMA trusted_schema"])
async def test_read_only_pragmas_still_describe_under_the_checker(checker: Connection, sql: str) -> None:
    await describe(checker, sql)


@pytest.mark.asyncio
async def test_a_runtime_connection_still_sets_pragmas(dsn: str) -> None:
    conn = await connect(ConnectOptions(path=dsn.removeprefix("sqlite:///"), mode="rw"))
    try:
        await conn._impl._execute("PRAGMA recursive_triggers = ON", [])
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_a_syntax_error_reports_a_position(checker: Connection) -> None:
    with pytest.raises(DatabaseError) as raised:
        await describe(checker, "SELECT a FROM t WHERE )")
    assert raised.value.position is not None
    with pytest.raises(DatabaseError) as unknown:
        await describe(checker, "SELECT nope FROM t")
    assert unknown.value.position == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("sql", ["", "   ", "-- nothing", "SELECT 1; SELECT 2", "SELECT 1; DELETE FROM t"])
async def test_exactly_one_statement_is_described(checker: Connection, sql: str) -> None:
    with pytest.raises(DatabaseError) as raised:
        await describe(checker, sql)
    assert (raised.value.sqlstate, raised.value.code, raised.value.position) == ("HY000", None, None)


@pytest.mark.asyncio
async def test_a_trailing_semicolon_or_comment_is_not_a_second_statement(checker: Connection) -> None:
    for sql in ["SELECT a FROM t;", "SELECT a FROM t; -- done", "SELECT a FROM t /* done */"]:
        assert len((await describe(checker, sql)).columns) == 1


@pytest.mark.asyncio
async def test_an_aggregate_without_group_by_returns_a_null_row_over_an_empty_table(dsn: str) -> None:
    writer = await connect(ConnectOptions(path=dsn.removeprefix("sqlite:///"), mode="rw"))
    try:
        rows = await writer._impl._fetch("SELECT a, count(*) FROM t", [], None, False, False, 2, None)  # ruff: ignore[boolean-positional-value-in-call]
        assert rows == [(None, 0)]
    finally:
        await writer.close()


@pytest.mark.asyncio
async def test_a_primary_key_table_constraint_is_a_rowid_alias(dsn: str) -> None:
    writer = await connect(ConnectOptions(path=dsn.removeprefix("sqlite:///"), mode="rw"))
    try:
        await writer._impl._execute("INSERT INTO pk2 (id, v) VALUES (NULL, 'x')", [])
        rows = await writer._impl._fetch("SELECT id FROM pk2", [], None, True, False, None, None)  # ruff: ignore[boolean-positional-value-in-call]
        assert rows == [1]
    finally:
        await writer.close()


@pytest.mark.asyncio
async def test_an_unknown_construct_name_raises(checker: Connection) -> None:
    with pytest.raises(ValueError, match="unknown construct"):
        await describe_impl(checker, "SELECT a FROM t", [("t",)], constructs=["bogus"])  # ty: ignore[invalid-argument-type]

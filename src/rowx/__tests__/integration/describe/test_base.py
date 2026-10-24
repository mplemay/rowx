import asyncio
from typing import TYPE_CHECKING

import pytest

from rowx._core import Describe, connect_checker, describe
from rowx.errors import DatabaseError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from rowx._core import Connection

pytestmark = [pytest.mark.integration, pytest.mark.db]


def run_describe(dsn: str, sql: str) -> Describe:
    async def body(conn: Connection) -> Describe:
        return await describe(conn, sql)

    return run_checker(dsn, body)


def run_checker[T](dsn: str, body: Callable[[Connection], Awaitable[T]]) -> T:
    async def run() -> T:
        conn = await connect_checker(dsn)
        try:
            return await body(conn)
        finally:
            await conn.close()

    return asyncio.run(run())


async def scalar(conn: Connection, sql: str) -> object:
    rows = await conn._fetch(sql, [], None, True, True, None)  # noqa: FBT003
    return rows[0]


def test_params_and_columns(schema: str) -> None:
    d = run_describe(schema, "SELECT id, name FROM users WHERE id = $1")
    assert [(p.type_name, p.type_key) for p in d.params] == [("int4", "int4")]
    assert [(c.name, c.raw_name, c.type_name, c.type_key) for c in d.columns] == [
        ("id", "id", "int4", "int4"),
        ("name", "name", "text", "text"),
    ]
    assert all(c.table_oid is not None and c.attnum is not None for c in d.columns)
    assert [c.attnum for c in d.columns] == [1, 2]


def test_computed_column_has_no_origin(schema: str) -> None:
    (col,) = run_describe(schema, "SELECT 1 AS one").columns
    assert (col.table_oid, col.attnum, col.type_key) == (None, None, "int4")


def test_bad_sql_raises_with_position(schema: str) -> None:
    with pytest.raises(DatabaseError) as info:
        run_describe(schema, "SELECT FROM WHERE")
    assert info.value.sqlstate == "42601"
    assert info.value.position == 13


def test_describe_does_not_execute_insert(schema: str) -> None:
    async def body(conn: Connection) -> tuple[object, object]:
        count = "SELECT count(*) FROM users"
        before = await scalar(conn, count)
        d = await describe(conn, "INSERT INTO users (name) VALUES ($1) RETURNING id")
        assert [c.name for c in d.columns] == ["id"]
        after = await scalar(conn, count)
        return before, after

    before, after = run_checker(schema, body)
    assert before == after


def test_multiple_statements_rejected(schema: str) -> None:
    with pytest.raises(DatabaseError):
        run_describe(schema, "SELECT 1; SELECT 2")


def test_unsupported_types(schema: str) -> None:
    d = run_describe(schema, "SELECT c_money, NULL::aclitem AS acl, c_inet FROM unsupported_types")
    assert [c.type_key for c in d.columns] == [None, None, None]
    assert [c.type_name for c in d.columns] == ["money", "aclitem", "inet"]


def test_enum_citext_domain_resolve(schema: str) -> None:
    d = run_describe(
        schema,
        "SELECT c_mood, c_citext, c_domain, c_domain_array, c_mood_array, c_int4_matrix FROM all_types",
    )
    # the server describes a domain column by its base type, so a domain resolves to that type's key
    assert [c.type_key for c in d.columns] == ["enum", "citext", "int4", "array", "array", "array"]


def test_domain_param_resolves_to_its_base_key(schema: str) -> None:
    d = run_describe(schema, "SELECT $1::positive_int")
    assert [(p.type_name, p.type_key) for p in d.params] == [("positive_int", "int4")]


def test_unlisted_type_with_a_builtin_send_function_is_unsupported(schema: str) -> None:
    # refcursor reuses textsend, but the runtime has no decoder for it
    (col,) = run_describe(schema, "SELECT 'c'::refcursor AS cur").columns
    assert (col.type_name, col.type_key) == ("refcursor", None)


def test_checker_session_settings(schema: str) -> None:
    async def body(conn: Connection) -> list[object]:
        names = ("application_name", "default_transaction_read_only", "lock_timeout")
        return [await scalar(conn, f"SHOW {name}") for name in names]

    assert run_checker(schema, body) == ["rowx-check", "on", "2s"]


def test_statement_timeout_applied(schema: str) -> None:
    async def run() -> object:
        conn = await connect_checker(schema, 2.5)
        try:
            return await scalar(conn, "SHOW statement_timeout")
        finally:
            await conn.close()

    assert asyncio.run(run()) == "2500ms"


def test_array_columns_carry_their_element_key(schema: str) -> None:
    d = run_describe(schema, "SELECT ARRAY[1] AS a, ARRAY['x']::text[] AS b, 1 AS c")
    assert [(c.type_key, c.element_key) for c in d.columns] == [("array", "int4"), ("array", "text"), ("int4", None)]

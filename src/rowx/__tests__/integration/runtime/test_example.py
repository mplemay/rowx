import asyncio
from decimal import Decimal

import pytest

import rowx
from rowx import QueryResult, query, query_unchecked

pytestmark = [pytest.mark.integration, pytest.mark.db]

type UserRow = tuple[int, str, str | None]


def test_design_example(schema: str) -> None:
    async def main() -> None:
        async with await rowx.connect(schema) as conn:
            uid = await query[int](t"SELECT id FROM users WHERE name = {'alice'}").one(conn)
            user = await query[UserRow](t"SELECT id, name, email FROM users WHERE id = {uid}").one(conn)
            assert user == (uid, "alice", "alice@example.com")

            names = await query[str](t"SELECT name FROM users ORDER BY name").all(conn)
            assert names == ["alice", "bob", "carol"]

            ids = [uid, uid + 1, uid + 2]
            n = await query(t"UPDATE users SET name = {'x'} WHERE id = ANY({ids})").execute(conn)
            assert n == QueryResult(rows_affected=3)

            total = await query[Decimal](t'SELECT sum(amount) AS "total!" FROM orders').one(conn)
            assert total == Decimal("110.49")

    asyncio.run(main())


def test_execute_on_typed_query_discards_rows(schema: str) -> None:
    async def body() -> QueryResult:
        async with await rowx.connect(schema) as conn:
            return await query[tuple[int, str]](t"SELECT id, name FROM users").execute(conn)

    assert asyncio.run(body()) == QueryResult(rows_affected=3)


def test_query_unchecked_nested_template_and_ident(schema: str) -> None:
    async def body() -> list[str]:
        table, column = "users", "name"
        cond = t"WHERE {'id':ident} > {0}"
        async with await rowx.connect(schema) as conn:
            q = query_unchecked[str](t"SELECT {column:ident} FROM {table:ident} {cond} ORDER BY 1")
            return await q.all(conn)

    assert asyncio.run(body()) == ["alice", "bob", "carol"]


def test_query_unchecked_qualified_ident(schema: str) -> None:
    async def body() -> int:
        async with await rowx.connect(schema) as conn:
            return await query_unchecked[int](t"SELECT count(*) FROM {('pg_catalog', 'pg_class'):ident}").one(
                conn,
            )

    assert asyncio.run(body()) > 0

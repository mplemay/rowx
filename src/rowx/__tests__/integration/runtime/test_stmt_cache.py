import asyncio
from typing import TYPE_CHECKING, Final

import pytest

import rowx
from rowx.errors import DatabaseError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from rowx._core.postgres import Connection as ConnectionImpl

pytestmark = [pytest.mark.integration, pytest.mark.db]

PREPARED_COUNT: Final[str] = "SELECT count(*)::int4 FROM pg_prepared_statements"


async def fetch(conn: ConnectionImpl, sql: str, *, scalar: bool = False) -> list[object]:
    mask = [False] if scalar else None
    return await conn._fetch(sql, [], mask, scalar, scalar, 1 if scalar else None)


async def fetch_rows(conn: ConnectionImpl, sql: str, mask: list[bool]) -> list[object]:
    scalar = limit_one = False
    return await conn._fetch(sql, [], mask, scalar, limit_one, len(mask))


async def prepared_count(conn: ConnectionImpl) -> int:
    (count,) = await fetch(conn, PREPARED_COUNT, scalar=True)
    assert isinstance(count, int)
    return count


def run_with_conn[T](dsn: str, body: Callable[[ConnectionImpl], Awaitable[T]], *, cache_size: int = 256) -> T:
    async def run() -> T:
        conn = await rowx.connect(dsn, statement_cache_size=cache_size)
        try:
            return await body(conn._impl)
        finally:
            await conn.close()

    return asyncio.run(run())


def test_cache_is_bounded(schema: str) -> None:
    async def body(c: ConnectionImpl) -> int:
        for i in range(300):
            await fetch(c, f"SELECT {i}")
        return await prepared_count(c)

    assert run_with_conn(schema, body) <= 256


def test_cache_size_zero_keeps_nothing_prepared(schema: str) -> None:
    async def body(c: ConnectionImpl) -> int:
        for i in range(5):
            await fetch(c, f"SELECT {i}")
        return await prepared_count(c)

    assert run_with_conn(schema, body, cache_size=0) <= 1


def test_schema_change_retries_outside_a_transaction(schema: str) -> None:
    async def body(c: ConnectionImpl) -> list[object]:
        await c._execute("CREATE TABLE cached_t (a int4)", [])
        await c._execute("INSERT INTO cached_t VALUES (1)", [])
        assert await fetch(c, "SELECT * FROM cached_t") == [(1,)]
        await c._execute("ALTER TABLE cached_t ADD COLUMN b int4 DEFAULT 2", [])
        return await fetch(c, "SELECT * FROM cached_t")

    assert run_with_conn(schema, body) == [(1, 2)]


def test_schema_change_reprepares_before_rejecting_a_new_row_shape(schema: str) -> None:
    async def body(c: ConnectionImpl) -> list[object]:
        sql = "SELECT * FROM cached_t"
        await c._execute("CREATE TABLE cached_t (a int4)", [])
        await c._execute("INSERT INTO cached_t VALUES (1)", [])
        assert await fetch_rows(c, sql, [False]) == [(1,)]
        await c._execute("ALTER TABLE cached_t ADD COLUMN b int4 DEFAULT 2", [])
        return await fetch_rows(c, sql, [False, False])

    assert run_with_conn(schema, body) == [(1, 2)]


def test_schema_change_inside_a_transaction_raises_with_hint(schema: str) -> None:
    async def body(c: ConnectionImpl) -> DatabaseError:
        await c._execute("CREATE TABLE cached_t (a int4)", [])
        await fetch(c, "SELECT * FROM cached_t")
        await c._execute("BEGIN", [])
        await c._execute("ALTER TABLE cached_t ADD COLUMN b int4", [])
        try:
            with pytest.raises(DatabaseError) as info:
                await fetch(c, "SELECT * FROM cached_t")
        finally:
            await c._execute("ROLLBACK", [])
        return info.value

    err = run_with_conn(schema, body)
    assert err.sqlstate == "0A000"
    assert err.hint == "schema changed inside a transaction; ROLLBACK and retry"


def test_discard_all_clears_the_cache_and_retries(schema: str) -> None:
    async def body(c: ConnectionImpl) -> list[object]:
        assert await fetch(c, "SELECT 1") == [(1,)]
        await c._execute("DISCARD ALL", [])
        return await fetch(c, "SELECT 1")

    assert run_with_conn(schema, body) == [(1,)]

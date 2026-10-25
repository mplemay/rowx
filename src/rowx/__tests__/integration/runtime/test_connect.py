import asyncio
import inspect

import pytest
from psycopg.conninfo import make_conninfo

from rowx.errors import DatabaseError, InterfaceError
from rowx.postgres import Connection, connect

pytestmark = [pytest.mark.integration, pytest.mark.db]


def _with_password(dsn: str, password: str) -> str:
    return make_conninfo(dsn, password=password)


def test_connect_and_close(schema: str) -> None:
    async def run() -> tuple[bool, bool, bool]:
        conn = await connect(schema)
        before = conn.closed
        await conn.close()
        await conn.close()
        return before, conn.closed, isinstance(conn, Connection)

    assert asyncio.run(run()) == (False, True, True)


def test_async_with_closes(schema: str) -> None:
    async def run() -> tuple[bool, bool]:
        async with await connect(schema, connect_timeout=5, statement_cache_size=0) as conn:
            inside = conn.closed
        return inside, conn.closed

    assert asyncio.run(run()) == (False, True)


def test_malformed_dsn_is_interface_error() -> None:
    async def run() -> None:
        await connect("postgresql://[bad")

    with pytest.raises(InterfaceError):
        asyncio.run(run())


def test_negative_timeout_is_interface_error() -> None:
    async def run() -> None:
        await connect("postgresql://localhost/x", connect_timeout=-1)

    with pytest.raises(InterfaceError):
        asyncio.run(run())


def test_bad_password_is_database_error(schema: str) -> None:
    async def run() -> None:
        await connect(_with_password(schema, "definitely-wrong"))

    with pytest.raises(DatabaseError) as info:
        asyncio.run(run())
    assert info.value.sqlstate == "28P01"


def test_connect_in_task(schema: str) -> None:
    async def run() -> bool:
        conn = await asyncio.create_task(connect(schema))
        await conn.close()
        return conn.closed

    assert asyncio.run(run())


def test_close_is_coroutine_function() -> None:
    assert inspect.iscoroutinefunction(Connection.close)
    assert inspect.iscoroutinefunction(connect)

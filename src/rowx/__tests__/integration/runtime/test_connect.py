import asyncio
import inspect
from urllib.parse import urlparse, urlunparse

import pytest

import rowx
from rowx.errors import DatabaseError, InterfaceError

pytestmark = [pytest.mark.integration, pytest.mark.db]


def _with_password(dsn: str, password: str) -> str:
    parts = urlparse(dsn)
    host = parts.hostname or "127.0.0.1"
    port = "" if parts.port is None else f":{parts.port}"
    return urlunparse(parts._replace(netloc=f"{parts.username}:{password}@{host}{port}"))


def test_connect_and_close(schema: str) -> None:
    async def run() -> tuple[bool, bool, bool]:
        conn = await rowx.connect(schema)
        before = conn.closed
        await conn.close()
        await conn.close()
        return before, conn.closed, isinstance(conn, rowx.Connection)

    assert asyncio.run(run()) == (False, True, True)


def test_async_with_closes(schema: str) -> None:
    async def run() -> tuple[bool, bool]:
        async with await rowx.connect(schema, connect_timeout=5, statement_cache_size=0) as conn:
            inside = conn.closed
        return inside, conn.closed

    assert asyncio.run(run()) == (False, True)


def test_malformed_dsn_is_interface_error() -> None:
    async def run() -> None:
        await rowx.connect("postgresql://[bad")

    with pytest.raises(InterfaceError):
        asyncio.run(run())


def test_negative_timeout_is_interface_error() -> None:
    async def run() -> None:
        await rowx.connect("postgresql://localhost/x", connect_timeout=-1)

    with pytest.raises(InterfaceError):
        asyncio.run(run())


def test_bad_password_is_database_error(schema: str) -> None:
    async def run() -> None:
        await rowx.connect(_with_password(schema, "definitely-wrong"))

    with pytest.raises(DatabaseError) as info:
        asyncio.run(run())
    assert info.value.sqlstate == "28P01"


def test_connect_in_task(schema: str) -> None:
    async def run() -> bool:
        conn = await asyncio.create_task(rowx.connect(schema))
        await conn.close()
        return conn.closed

    assert asyncio.run(run())


def test_close_is_coroutine_function() -> None:
    assert inspect.iscoroutinefunction(rowx.Connection.close)
    assert inspect.iscoroutinefunction(rowx.connect)

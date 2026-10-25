import asyncio
from typing import TYPE_CHECKING, Final

import pytest

from rowx.errors import ConnectionBusyError, DatabaseError
from rowx.postgres import connect

if TYPE_CHECKING:
    from rowx._core.postgres import Connection as ConnectionImpl

pytestmark = [pytest.mark.integration, pytest.mark.db, pytest.mark.timing]

SLEEPING: Final[str] = (
    "SELECT count(*)::int4 FROM pg_stat_activity WHERE query LIKE '%pg_sleep(1)%' AND pid <> pg_backend_pid()"
)
POLL_LIMIT: Final[int] = 2000


async def fetch(conn: ConnectionImpl, sql: str, *, scalar: bool = False) -> list[object]:
    mask = [False] if scalar else None
    return await conn._fetch(sql, [], mask, scalar, scalar, 1 if scalar else None)


async def select_one(conn: ConnectionImpl) -> list[object]:
    return await fetch(conn, "SELECT 1", scalar=True)


async def sleep_one(conn: ConnectionImpl) -> list[object]:
    return await fetch(conn, "SELECT pg_sleep(1)")


async def wait_until_running(observer: ConnectionImpl) -> None:
    # each poll is a round trip on the observer, so no sleep is needed
    for _ in range(POLL_LIMIT):
        (count,) = await fetch(observer, SLEEPING, scalar=True)
        if count:
            return
    pytest.fail("the sleeping query never showed up in pg_stat_activity")


def test_second_call_while_busy_raises_then_next_call_succeeds(schema: str) -> None:
    async def run() -> list[object]:
        conn = await connect(schema)
        observer = await connect(schema)
        try:
            slow = asyncio.create_task(sleep_one(conn._impl))
            await wait_until_running(observer._impl)
            with pytest.raises(ConnectionBusyError):
                await select_one(conn._impl)
            await slow
            return await select_one(conn._impl)
        finally:
            await conn.close()
            await observer.close()

    assert asyncio.run(run()) == [1]


def test_sequential_awaits_never_raise_busy(schema: str) -> None:
    async def run() -> int:
        conn = await connect(schema)
        try:
            for _ in range(1000):
                await select_one(conn._impl)
                await select_one(conn._impl)
            return 1000
        finally:
            await conn.close()

    assert asyncio.run(run()) == 1000


def test_error_result_leaves_the_connection_idle(schema: str) -> None:
    async def run() -> list[object]:
        conn = await connect(schema)
        try:
            with pytest.raises(DatabaseError):
                await fetch(conn._impl, "SELECT 1/0")
            return await select_one(conn._impl)
        finally:
            await conn.close()

    assert asyncio.run(run()) == [1]

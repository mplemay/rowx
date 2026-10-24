import asyncio
import contextlib
import random
import time
from typing import TYPE_CHECKING, Final

import pytest

import rowx
from rowx._core import _cancel_requests_sent
from rowx.errors import DatabaseError

if TYPE_CHECKING:
    from rowx._core import Connection as ConnectionImpl

pytestmark = [pytest.mark.integration, pytest.mark.db, pytest.mark.timing]

SLEEPING: Final[str] = (
    "SELECT count(*)::int4 FROM pg_stat_activity "
    "WHERE state = 'active' AND query LIKE '%pg_sleep(3)%' AND pid <> pg_backend_pid()"
)
GONE_WITHIN: Final[float] = 2.0
STRESS_ITERATIONS: Final[int] = 300


async def fetch(conn: ConnectionImpl, sql: str, *, scalar: bool = False) -> list[object]:
    mask = [False] if scalar else None
    return await conn._fetch(sql, [], mask, scalar, scalar, 1 if scalar else None)


async def select_one(conn: ConnectionImpl) -> list[object]:
    return await fetch(conn, "SELECT 1", scalar=True)


async def sleeping(observer: ConnectionImpl) -> int:
    (count,) = await fetch(observer, SLEEPING, scalar=True)
    assert isinstance(count, int)
    return count


async def wait_for_sleep(observer: ConnectionImpl, *, present: bool, limit: float) -> bool:
    # each poll is a round trip on the observer, so no sleep is needed
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if bool(await sleeping(observer)) == present:
            return True
    return False


def test_cancelled_sleep_frees_the_backend_and_the_next_query_succeeds(schema: str) -> None:
    async def run() -> list[object]:
        conn = await rowx.connect(schema)
        observer = await rowx.connect(schema)
        try:
            slow = asyncio.create_task(fetch(conn._impl, "SELECT pg_sleep(3)"))
            assert await wait_for_sleep(observer._impl, present=True, limit=GONE_WITHIN)
            slow.cancel()
            with pytest.raises(asyncio.CancelledError):
                await slow
            assert await wait_for_sleep(observer._impl, present=False, limit=GONE_WITHIN)
            return await select_one(conn._impl)
        finally:
            await conn.close()
            await observer.close()

    assert asyncio.run(run()) == [1]


def test_timeout_cancel_after_100ms(schema: str) -> None:
    async def run() -> list[object]:
        conn = await rowx.connect(schema)
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(fetch(conn._impl, "SELECT pg_sleep(3)"), 0.1)
            return await select_one(conn._impl)
        finally:
            await conn.close()

    assert asyncio.run(run()) == [1]


def test_stress_cancel_then_query_never_fails_with_57014(schema: str) -> None:
    async def run() -> list[str]:
        conn = await rowx.connect(schema)
        failures: list[str] = []
        rng = random.Random(22)  # ruff: ignore[suspicious-non-cryptographic-random-usage]
        try:
            for _ in range(STRESS_ITERATIONS):
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(fetch(conn._impl, "SELECT pg_sleep(0.004)"), rng.uniform(0.002, 0.006))
                try:
                    await select_one(conn._impl)
                except DatabaseError as exc:
                    failures.append(exc.sqlstate)
            return failures
        finally:
            await conn.close()

    assert asyncio.run(run()) == []


def test_sql_error_sends_no_cancel_request(schema: str) -> None:
    async def run() -> int:
        conn = await rowx.connect(schema)
        try:
            before = _cancel_requests_sent()
            with pytest.raises(DatabaseError):
                await fetch(conn._impl, "SELECT 1/0")
            await select_one(conn._impl)
            return _cancel_requests_sent() - before
        finally:
            await conn.close()

    assert asyncio.run(run()) == 0


def test_cancelling_a_large_fetch_leaves_the_connection_usable(schema: str) -> None:
    async def run() -> list[object]:
        conn = await rowx.connect(schema)
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(fetch(conn._impl, "SELECT g FROM generate_series(1, 1000000) g"), 0.01)
            return await select_one(conn._impl)
        finally:
            await conn.close()

    assert asyncio.run(run()) == [1]


def test_close_during_a_long_query_returns_quickly(schema: str) -> None:
    async def run() -> float:
        conn = await rowx.connect(schema)
        observer = await rowx.connect(schema)
        try:
            slow = asyncio.create_task(fetch(conn._impl, "SELECT pg_sleep(3)"))
            assert await wait_for_sleep(observer._impl, present=True, limit=GONE_WITHIN)
            started = time.monotonic()
            await conn.close()
            elapsed = time.monotonic() - started
            with pytest.raises(DatabaseError):
                await slow
            return elapsed
        finally:
            await observer.close()

    assert asyncio.run(run()) < GONE_WITHIN

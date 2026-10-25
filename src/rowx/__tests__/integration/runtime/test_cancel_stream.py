import asyncio
import contextlib
import random
import time
from typing import Final

import pytest

import rowx
from rowx import Connection, InterfaceError, query

pytestmark = [pytest.mark.integration, pytest.mark.db, pytest.mark.timing]

GONE_WITHIN: Final[float] = 2.0
STRESS_ITERATIONS: Final[int] = 300


async def wait_for_sleep(observer: Connection, *, present: bool, limit: float) -> bool:
    # each poll is a round trip on the observer, so no sleep is needed
    deadline = time.monotonic() + limit
    probe = query[int](
        t"SELECT count(*)::int4 FROM pg_stat_activity "
        t"WHERE state = 'active' AND query LIKE '%pg_sleep(3)%' AND pid <> pg_backend_pid()",
    )
    while time.monotonic() < deadline:
        if bool(await probe.one(observer)) == present:
            return True
    return False


async def consume_slow(conn: Connection) -> None:
    slow = query[str](t"SELECT pg_sleep(3)::text")
    async for _ in slow.fetch(conn):
        pass


def test_cancelled_anext_on_slow_query_frees_the_backend(schema: str) -> None:
    async def run() -> int:
        conn = await rowx.connect(schema)
        observer = await rowx.connect(schema)
        try:
            task = asyncio.create_task(consume_slow(conn))
            assert await wait_for_sleep(observer, present=True, limit=GONE_WITHIN)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await wait_for_sleep(observer, present=False, limit=GONE_WITHIN)
            return await query[int](t"SELECT 1").one(conn)
        finally:
            await conn.close()
            await observer.close()

    assert asyncio.run(run()) == 1


def test_cancelled_stream_that_stays_referenced_ends_and_frees_the_connection(schema: str) -> None:
    async def run() -> tuple[int, bool]:
        conn = await rowx.connect(schema)
        observer = await rowx.connect(schema)
        try:
            stream = query[str](t"SELECT pg_sleep(3)::text").fetch(conn)
            task = asyncio.create_task(anext(stream))
            assert await wait_for_sleep(observer, present=True, limit=GONE_WITHIN)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await wait_for_sleep(observer, present=False, limit=GONE_WITHIN)
            # the stream is still referenced, yet the connection is usable and the stream is over
            ok = await query[int](t"SELECT 1").one(conn)
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
            return ok, stream.result is None
        finally:
            await conn.close()
            await observer.close()

    assert asyncio.run(run()) == (1, True)


def test_close_during_a_pending_anext_raises_interface_error(schema: str) -> None:
    async def run() -> None:
        conn = await rowx.connect(schema)
        observer = await rowx.connect(schema)
        try:
            pending = asyncio.create_task(anext(query[str](t"SELECT pg_sleep(3)::text").fetch(conn)))
            assert await wait_for_sleep(observer, present=True, limit=GONE_WITHIN)
            await conn.close()
            with pytest.raises(InterfaceError):
                await pending
        finally:
            await observer.close()

    asyncio.run(run())


def test_stream_cancel_stress_follow_up_query_never_fails(schema: str) -> None:
    async def one_stream(conn: Connection) -> None:
        async with query[int](t"SELECT g FROM generate_series(1, 200000) g").fetch(conn, batch_size=50) as rows:
            async for _ in rows:
                pass

    async def run() -> list[str]:
        conn = await rowx.connect(schema)
        failures: list[str] = []
        rng = random.Random(63)  # ruff: ignore[suspicious-non-cryptographic-random-usage]
        try:
            for _ in range(STRESS_ITERATIONS):
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(one_stream(conn), rng.uniform(0.0005, 0.02))
                try:
                    await query[int](t"SELECT 1").one(conn)
                except rowx.Error as exc:
                    failures.append(repr(exc))
            return failures
        finally:
            await conn.close()

    assert asyncio.run(run()) == []

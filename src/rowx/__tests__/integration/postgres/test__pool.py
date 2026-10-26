import asyncio
import contextlib
import inspect
import os
import random
import warnings
from collections import Counter
from typing import TYPE_CHECKING, Final

import pytest

from rowx import query, query_unchecked
from rowx.errors import ConnectionBusyError, DatabaseError, InterfaceError, PoolTimeoutError
from rowx.postgres import RESET_QUERY, Connection, Pool, connect

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = [pytest.mark.integration, pytest.mark.db]

POLL_LIMIT: Final[int] = 2000
SPIN_LIMIT: Final[int] = 200_000
STRESS_ITERATIONS: Final[int] = 300
ADVISORY_KEY: Final[int] = 727_002
UNREACHABLE_DSN: Final[str] = "postgresql://rowx:rowx@127.0.0.1:1/rowx"


async def backend_pid(conn: Connection) -> int:
    return await query[int](t"SELECT pg_backend_pid()").one(conn)


async def wait_active(observer: Connection, pid: int) -> None:
    # each poll is a round trip on the observer, so no sleep is needed
    for _ in range(POLL_LIMIT):
        state = await query[str | None](t"SELECT state FROM pg_stat_activity WHERE pid = {pid}").optional(observer)
        if state == "active":
            return
    pytest.fail("the query never became active")


async def spin_until(condition: Callable[[], bool]) -> None:
    # yields to the loop (and so to the runtime threads) without sleeping
    for _ in range(SPIN_LIMIT):
        if condition():
            return
        await asyncio.sleep(0)
    pytest.fail("the condition never became true")


def test_stream_created_in_acquire_and_iterated_after_it_raises(schema: str) -> None:
    async def run() -> tuple[int, int]:
        async with Pool(schema, max_size=1) as pool:
            async with pool.acquire() as conn:
                stream = query[int](t"SELECT g FROM generate_series(1, 5) g").fetch(conn)
                raw = conn._impl._stream("SELECT g FROM generate_series(1, 5) g", [], [False], True, 1, 10)  # ruff: ignore[boolean-positional-value-in-call]
            with pytest.raises(InterfaceError):
                _ = [row async for row in stream]
            # the next holder gets the same backend, and the stale stream cannot claim it
            async with pool.acquire() as next_conn:
                with pytest.raises(InterfaceError):
                    await raw._next()
                first = await query[int](t"SELECT 1").one(next_conn)
            return first, await query[int](t"SELECT 2").one(pool)

    assert asyncio.run(run()) == (1, 2)


@pytest.mark.timing
def test_release_cancels_and_settles_a_running_background_query(schema: str) -> None:
    async def run() -> int:
        async with Pool(schema, max_size=1, acquire_timeout=10) as pool, await connect(schema) as observer:
            async with pool.acquire() as conn:
                pid = await backend_pid(conn)
                background = asyncio.create_task(query(t"SELECT pg_sleep(30)").execute(conn))
                await wait_active(observer, pid)
            with pytest.raises((DatabaseError, InterfaceError)):
                await background
            return await query[int](t"SELECT 7").one(pool)

    assert asyncio.run(run()) == 7


def test_init_runs_for_a_connection_warmed_by_open(schema: str) -> None:
    seen: list[int] = []

    async def init(conn: Connection) -> None:
        seen.append(await backend_pid(conn))

    async def run() -> int:
        async with Pool(schema, min_size=1, max_size=2, init=init) as pool:
            assert seen == []
            async with pool.acquire() as conn:
                return await backend_pid(conn)

    pid = asyncio.run(run())
    assert seen == [pid]


def test_reset_hook_works_across_two_event_loops(schema: str) -> None:
    resets: list[int] = []

    async def reset(conn: Connection) -> None:
        resets.append(await query[int](t"SELECT 1").one(conn))

    pool = Pool(schema, max_size=1, reset=reset)

    async def use() -> int:
        async with pool.acquire() as conn:
            return await query[int](t"SELECT 5").one(conn)

    assert asyncio.run(use()) == 5
    assert asyncio.run(use()) == 5
    asyncio.run(pool.close())
    assert resets == [1, 1]


def test_setup_that_raises_discards_the_connection(schema: str) -> None:
    pids: list[int] = []

    async def setup(conn: Connection) -> None:
        pids.append(await backend_pid(conn))
        if len(pids) == 1:
            msg = "setup failed"
            raise RuntimeError(msg)

    async def run() -> tuple[int, int]:
        async with Pool(schema, max_size=1, setup=setup) as pool:
            with pytest.raises(RuntimeError, match="setup failed"):
                await pool.acquire()
            assert pool.size == 0
            async with pool.acquire() as conn:
                return pids[0], await backend_pid(conn)

    first, second = asyncio.run(run())
    assert first != second


def test_pool_size_stays_correct_while_an_early_exited_stream_drains(schema: str) -> None:
    async def run() -> tuple[int, int, int]:
        async with Pool(schema, max_size=1, acquire_timeout=30) as pool:
            stream = query[int](t"SELECT g FROM generate_series(1, 2000000) g").fetch(pool, batch_size=10)
            async for _ in stream:
                break
            await stream.aclose()
            held = pool.size
            assert pool.size <= pool.max_size
            nxt = await query[int](t"SELECT 1").one(pool)
            return held, nxt, pool.size

    assert asyncio.run(run()) == (1, 1, 1)


def test_pooled_connections_recover_after_a_migration_through_the_schema_epoch(schema: str) -> None:
    async def run() -> list[int]:
        table = query_unchecked[int](t"SELECT a FROM epoch_t")
        async with Pool(schema, max_size=2) as pool, await connect(schema) as admin:
            await query(t"CREATE TABLE epoch_t (a int4)").execute(admin)
            await query(t"INSERT INTO epoch_t VALUES (1)").execute(admin)
            # one cached plan on each of the two connections
            async with pool.acquire() as first, pool.acquire() as second:
                await table.one(first)
                await table.one(second)
            await query(t"ALTER TABLE epoch_t ALTER COLUMN a TYPE int8").execute(admin)
            # inside a transaction a stale plan cannot be retried, so it fails and bumps the epoch
            async with pool.acquire() as first, pool.acquire():
                await query(t"BEGIN").execute(first)
                with pytest.raises(DatabaseError):
                    await table.one(first)
            # every connection drops its statement cache at its next checkout, so neither fails again
            results: list[int] = []
            async with pool.acquire() as first, pool.acquire() as second:
                for conn in (first, second):
                    await query(t"BEGIN").execute(conn)
                    results.append(await table.one(conn))
            return results

    assert asyncio.run(run()) == [1, 1]


def test_a_forked_child_raises_an_interface_error(schema: str) -> None:
    pool = Pool(schema, max_size=1)

    async def child() -> int:
        try:
            await pool.open()
        except InterfaceError:
            pass
        else:
            return 1
        try:
            async with pool.acquire():
                pass
        except InterfaceError:
            return 0
        return 2

    with warnings.catch_warnings():
        # forking a process that has runtime threads is what this test is about
        warnings.simplefilter("ignore", DeprecationWarning)
        pid = os.fork()
    if pid == 0:
        code = 3
        try:
            code = asyncio.run(child())
        finally:
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0


def test_the_first_acquire_opens_the_pool(schema: str) -> None:
    async def run() -> tuple[int, int]:
        pool = Pool(schema, max_size=2)
        before = pool.size
        async with pool.acquire() as conn:
            await backend_pid(conn)
            after = pool.size
        await pool.close()
        return before, after

    assert asyncio.run(run()) == (0, 1)


def test_more_tasks_than_max_size_all_complete(schema: str) -> None:
    async def run() -> list[int]:
        async with Pool(schema, max_size=3, acquire_timeout=30) as pool:
            tasks = [query[int](t"SELECT {n}::int4 FROM (SELECT pg_sleep(0.005)) s").one(pool) for n in range(40)]
            results = await asyncio.gather(*tasks)
            assert pool.size <= 3
            return list(results)

    try:
        assert asyncio.run(run()) == list(range(40))
    except ConnectionBusyError:  # pragma: no cover
        pytest.fail("a pool must never raise ConnectionBusyError")


@pytest.mark.timing
def test_acquire_timeout_raises_pool_timeout_error(schema: str) -> None:
    async def run() -> None:
        async with (
            Pool(schema, max_size=1, acquire_timeout=0.2) as pool,
            await connect(schema) as observer,
            pool.acquire() as conn,
        ):
            pid = await backend_pid(conn)
            holder = asyncio.create_task(query(t"SELECT pg_sleep(1)").execute(conn))
            await wait_active(observer, pid)
            with pytest.raises(PoolTimeoutError):
                await query[int](t"SELECT 1").one(pool)
            with pytest.raises(PoolTimeoutError):
                await pool.acquire(timeout=0.05)
            await holder

    asyncio.run(run())


def test_using_a_connection_after_release_raises(schema: str) -> None:
    async def run() -> None:
        async with Pool(schema, max_size=1) as pool:
            async with pool.acquire() as conn:
                await backend_pid(conn)
            with pytest.raises(InterfaceError):
                await query[int](t"SELECT 1").one(conn)
            with pytest.raises(InterfaceError):
                await conn.ping()
            with pytest.raises(InterfaceError):
                await pool.release(conn)

    asyncio.run(run())


@pytest.mark.parametrize("reset_query_off", [False, True])
def test_session_state_does_not_leak_between_checkouts(schema: str, *, reset_query_off: bool) -> None:
    async def probe(conn: Connection) -> tuple[int, str, int, int, bool]:
        pid = await backend_pid(conn)
        locks = await query[int](
            t"SELECT count(*)::int4 FROM pg_locks WHERE locktype = 'advisory' AND pid = {pid}"
        ).one(conn)
        listening = await query[int](t"SELECT count(*)::int4 FROM pg_listening_channels()").one(conn)
        work_mem = await query[str](t"SHOW work_mem").one(conn)
        temp = await query[bool](t"SELECT to_regclass('pg_temp.leaked') IS NOT NULL").one(conn)
        return pid, work_mem, locks, listening, temp

    async def run() -> tuple[tuple[int, str, int, int, bool], tuple[int, str, int, int, bool]]:
        reset_query = None if reset_query_off else RESET_QUERY
        async with Pool(schema, max_size=1, reset_query=reset_query) as pool:
            async with pool.acquire() as conn:
                before = await probe(conn)
                await query(t"SET work_mem = '11MB'").execute(conn)
                await query(t"SELECT pg_advisory_lock({ADVISORY_KEY})").execute(conn)
                await query(t"LISTEN rowx_pool_test").execute(conn)
                await query(t"BEGIN").execute(conn)
                await query(t"CREATE TEMP TABLE leaked (id int)").execute(conn)
            async with pool.acquire() as conn:
                return before, await probe(conn)

    before, after = asyncio.run(run())
    assert before[0] == after[0]
    # an open transaction always ends, even without a reset query
    assert after[4] is False
    if reset_query_off:
        assert (after[1], after[2], after[3]) == ("11MB", 1, 1)
    else:
        assert after[1:4] == (before[1], 0, 0)


def test_hooks_run_once_per_connection_acquire_and_release(schema: str) -> None:
    counts: Counter[str] = Counter()

    async def init(_conn: Connection) -> None:
        await asyncio.sleep(0)
        counts["init"] += 1

    async def setup(_conn: Connection) -> None:
        await asyncio.sleep(0)
        counts["setup"] += 1

    async def reset(_conn: Connection) -> None:
        await asyncio.sleep(0)
        counts["reset"] += 1

    async def run() -> None:
        async with Pool(schema, max_size=1, init=init, setup=setup, reset=reset) as pool:
            for _ in range(3):
                async with pool.acquire() as conn:
                    await backend_pid(conn)

    asyncio.run(run())
    assert counts == {"init": 1, "setup": 3, "reset": 3}


def test_a_failing_init_surfaces_from_acquire_and_leaks_no_capacity(schema: str) -> None:
    calls: list[int] = []

    async def init(_conn: Connection) -> None:
        await asyncio.sleep(0)
        calls.append(1)
        if len(calls) == 1:
            msg = "init failed"
            raise RuntimeError(msg)

    async def run() -> tuple[int, int]:
        async with Pool(schema, max_size=1, acquire_timeout=5, init=init) as pool:
            with pytest.raises(RuntimeError, match="init failed"):
                await pool.acquire()
            assert (pool.size, pool.waiting) == (0, 0)
            async with pool.acquire() as conn:
                return await query[int](t"SELECT 3").one(conn), pool.size

    assert asyncio.run(run()) == (3, 1)


def test_a_stream_holds_the_connection_and_the_next_query_works_after_an_early_exit(schema: str) -> None:
    async def run() -> tuple[int, int]:
        async with Pool(schema, max_size=1, acquire_timeout=30) as pool:
            stream = query[int](t"SELECT g FROM generate_series(1, 100000) g").fetch(pool, batch_size=10)
            first = await anext(stream)
            assert pool.idle == 0
            waiting = asyncio.create_task(query[int](t"SELECT 1").one(pool))
            await spin_until(lambda: pool.waiting == 1)
            assert not waiting.done()
            await stream.aclose()
            return first, await waiting

    assert asyncio.run(run()) == (1, 1)


def test_cancelling_a_task_that_waits_in_acquire_leaks_nothing(schema: str) -> None:
    async def run() -> tuple[int, int, int]:
        async with Pool(schema, max_size=1, acquire_timeout=30) as pool:
            async with pool.acquire() as conn:
                await backend_pid(conn)
                waiter = asyncio.create_task(query[int](t"SELECT 1").one(pool))
                await spin_until(lambda: pool.waiting == 1)
                waiter.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiter
            value = await query[int](t"SELECT 2").one(pool)
            await spin_until(lambda: pool.idle == 1)
            return value, pool.size, pool.waiting

    assert asyncio.run(run()) == (2, 1, 0)


@pytest.mark.timing
def test_cancel_stress_on_a_pool_has_zero_failed_follow_ups(schema: str) -> None:
    async def run() -> int:
        rng = random.Random(727)  # ruff: ignore[suspicious-non-cryptographic-random-usage]
        failures = 0
        async with Pool(schema, max_size=2, acquire_timeout=30) as pool:
            for _ in range(STRESS_ITERATIONS):
                with contextlib.suppress(TimeoutError, DatabaseError):
                    await asyncio.wait_for(
                        query(t"SELECT pg_sleep(0.004)").execute(pool),
                        rng.uniform(0.002, 0.006),
                    )
                try:
                    if await query[int](t"SELECT 1").one(pool) != 1:
                        failures += 1
                except DatabaseError, InterfaceError:
                    failures += 1
        return failures

    assert asyncio.run(run()) == 0


def test_a_pool_built_outside_a_loop_works_across_two_runs(schema: str) -> None:
    pool = Pool(schema, max_size=2)

    async def use() -> int:
        return await query[int](t"SELECT 1").one(pool)

    assert asyncio.run(use()) == 1
    assert asyncio.run(use()) == 1
    asyncio.run(pool.close())


@pytest.mark.timing
def test_close_waits_for_checked_out_connections(schema: str) -> None:
    async def run() -> bool:
        pool = Pool(schema, max_size=1)
        async with await connect(schema) as observer:
            conn = await pool.acquire()
            pid = await backend_pid(conn)
            slow = asyncio.create_task(query(t"SELECT pg_sleep(0.3)").execute(conn))
            await wait_active(observer, pid)
            closing = asyncio.create_task(pool.close())
            await slow
            # the connection is still checked out, so the close is still waiting
            await spin_until(lambda: pool.closed)
            waiting = not closing.done()
            await conn.close()
            await closing
            with pytest.raises(InterfaceError):
                await pool.acquire()
        return waiting

    assert asyncio.run(run())


def test_pool_is_usable_through_the_shared_query_api(schema: str) -> None:
    async def run() -> tuple[int, list[int], int]:
        async with Pool(schema, max_size=2) as pool:
            one = await query[int](t"SELECT 1").one(pool)
            rows = [row async for row in query[int](t"SELECT g FROM generate_series(1, 3) g").fetch(pool)]
            result = await query(t"SELECT 1").execute(pool)
            return one, rows, result.rows_affected

    assert asyncio.run(run()) == (1, [1, 2, 3], 1)


@pytest.mark.parametrize("name", ["open", "close", "release", "_fetch", "_execute"])
def test_public_async_methods_are_coroutine_functions(name: str) -> None:
    assert inspect.iscoroutinefunction(getattr(Pool, name))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_size": 0},
        {"min_size": -1},
        {"min_size": 3, "max_size": 2},
        {"acquire_timeout": -1.0},
        {"idle_timeout": -1.0},
        {"max_lifetime": -1.0},
        {"ping_after": -1.0},
        {"max_queries": 0},
    ],
)
def test_bad_settings_raise_value_error(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="must"):
        Pool(UNREACHABLE_DSN, **kwargs)  # ty: ignore[invalid-argument-type]


def test_construction_does_no_io_and_exposes_the_settings() -> None:
    pool = Pool(UNREACHABLE_DSN, min_size=1, max_size=4)
    assert (pool.min_size, pool.max_size, pool.size, pool.idle, pool.waiting, pool.closed) == (1, 4, 0, 0, 0, False)

import asyncio
import contextlib
import inspect
import os
import random
import secrets
import warnings
from collections import Counter
from string.templatelib import Template
from typing import TYPE_CHECKING, Final

import psycopg
import pytest
from psycopg.sql import SQL, Identifier

from rowx import query, query_unchecked
from rowx.errors import ConnectionBusyError, DatabaseError, InterfaceError, PoolTimeoutError, RowShapeError
from rowx.postgres import RESET_QUERY, Connection, ConnectOptions, Pool, connect

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

pytestmark = [pytest.mark.integration, pytest.mark.db]

POLL_LIMIT: Final[int] = 2000
SPIN_LIMIT: Final[int] = 200_000
STRESS_ITERATIONS: Final[int] = 300
ADVISORY_KEY: Final[int] = 727_002
UNREACHABLE_DSN: Final[str] = "postgresql://rowx:rowx@127.0.0.1:1/rowx"


async def backend_pid(conn: Connection) -> int:
    return await query[int](t"SELECT pg_backend_pid()").one(conn)


async def first_drift(conn: Connection, match: str) -> None:
    # the report is processed right after the statement completes, so re-issue a call instead of sleeping
    failure: InterfaceError | None = None
    for _ in range(POLL_LIMIT):
        try:
            await backend_pid(conn)
        except InterfaceError as err:
            failure = err
            break
        await asyncio.sleep(0)
    assert failure is not None
    assert match in str(failure)


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
                raw = conn._impl._stream("SELECT g FROM generate_series(1, 5) g", [], [False], True, 1, 10, None)  # ruff: ignore[boolean-positional-value-in-call]
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


def test_a_hand_written_begin_through_the_pool_does_not_leak_a_transaction(schema: str) -> None:
    async def state_after_release(pool: Pool, observer: Connection, pid: int) -> str:
        # the release runs in the background, so wait until the connection is idle again
        await spin_until(lambda: pool.idle == 1)
        return await query[str](t"SELECT state FROM pg_stat_activity WHERE pid = {pid}").one(observer)

    async def run() -> tuple[str, str]:
        async with Pool(schema, max_size=1) as pool, await connect(schema) as observer:
            pid = await query[int](t"SELECT pg_backend_pid()").one(pool)
            await query(t"BEGIN").execute(pool)
            after_execute = await state_after_release(pool, observer, pid)
            # the statement runs, then its missing column fails the row plan
            with pytest.raises(RowShapeError):
                await query[int](t"BEGIN").optional(pool)
            return after_execute, await state_after_release(pool, observer, pid)

    assert asyncio.run(run()) == ("idle", "idle")


@pytest.mark.parametrize("hooked", [False, True])
def test_a_detached_connection_survives_the_end_of_the_acquire_block(schema: str, *, hooked: bool) -> None:
    async def noop(_conn: Connection) -> None:
        await asyncio.sleep(0)

    async def run() -> tuple[int, int]:
        async with Pool(schema, max_size=1, setup=noop if hooked else None) as pool:
            async with pool.acquire() as conn:
                conn.detach()
            size = pool.size
            value = await query[int](t"SELECT 9").one(conn)
            await conn.close()
            return size, value

    assert asyncio.run(run()) == (0, 9)


@pytest.mark.timing
def test_a_busy_connection_is_settled_and_reset_before_the_reset_hook_runs(schema: str) -> None:
    seen: list[str] = []

    async def reset(conn: Connection) -> None:
        seen.append(await query[str](t"SHOW work_mem").one(conn))

    async def run() -> str:
        async with Pool(schema, max_size=1, acquire_timeout=10, reset=reset) as pool, await connect(schema) as observer:
            async with pool.acquire() as conn:
                original = await query[str](t"SHOW work_mem").one(conn)
                await query(t"SET work_mem = '11MB'").execute(conn)
                pid = await backend_pid(conn)
                background = asyncio.create_task(query(t"SELECT pg_sleep(30)").execute(conn))
                await wait_active(observer, pid)
            with pytest.raises((DatabaseError, InterfaceError)):
                await background
            return original

    original = asyncio.run(run())
    assert seen == [original]


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


@pytest.mark.parametrize("hooked", [False, True])
def test_a_connection_that_drifted_is_replaced_after_release(schema: str, *, hooked: bool) -> None:
    async def reset(_conn: Connection) -> None:
        await asyncio.sleep(0)

    async def run() -> tuple[int, int, int, int]:
        async with Pool(schema, max_size=1, acquire_timeout=5, reset=reset if hooked else None) as pool:
            conn = await pool.acquire()
            first = await backend_pid(conn)
            with contextlib.suppress(InterfaceError):
                await query(t"SET client_encoding = 'SJIS'").execute(conn)
            await first_drift(conn, "client_encoding to SJIS")
            # the reset needs the connection, which fails, so it is discarded; the hook path says so
            if hooked:
                with pytest.raises(InterfaceError, match="SJIS"):
                    await pool.release(conn)
            else:
                await pool.release(conn)
            async with pool.acquire() as fresh:
                second = await backend_pid(fresh)
                assert await query[int](t"SELECT 2").one(fresh) == 2
                return first, second, pool.size, pool.waiting

    first, second, size, waiting = asyncio.run(run())
    assert first != second
    assert (size, waiting) == (1, 0)


def test_a_drifted_idle_connection_is_not_reused(schema: str) -> None:
    async def run() -> tuple[int, int]:
        async with Pool(schema, max_size=1, acquire_timeout=5) as pool:
            async with pool.acquire() as conn:
                first = await backend_pid(conn)
                with contextlib.suppress(InterfaceError):
                    await query(t"SET standard_conforming_strings = off").execute(conn)
            async with pool.acquire() as fresh:
                return first, await backend_pid(fresh)

    first, second = asyncio.run(run())
    assert first != second


@pytest.fixture
def hostile() -> Iterator[str]:
    # a schema `name` holding same-named functions and operators, and two roles `name_a` and `name_b`
    name = f"rowx_h_{secrets.token_hex(6)}"
    schema = Identifier(name)
    roles = [Identifier(f"{name}_a"), Identifier(f"{name}_b")]
    unlock = Identifier(name, "pg_advisory_unlock_all")
    always_true = Identifier(name, "always_true")
    setup = [
        *(SQL("CREATE ROLE {}").format(role) for role in roles),
        SQL("CREATE SCHEMA {}").format(schema),
        SQL("CREATE FUNCTION {}() RETURNS void LANGUAGE sql AS $$ SELECT 1 $$").format(unlock),
        SQL("CREATE FUNCTION {}(timestamptz, timestamptz) RETURNS boolean LANGUAGE sql AS $$ SELECT true $$").format(
            always_true
        ),
        SQL("CREATE OPERATOR {}.= (LEFTARG = timestamptz, RIGHTARG = timestamptz, FUNCTION = {})").format(
            schema, always_true
        ),
    ]
    with psycopg.connect(os.environ["ROWX_TEST_DSN"], autocommit=True) as admin:
        for statement in setup:
            admin.execute(statement)
        try:
            yield name
        finally:
            admin.execute(SQL("DROP SCHEMA {} CASCADE").format(schema))
            for role in roles:
                admin.execute(SQL("DROP ROLE {}").format(role))


async def identity(conn: Connection) -> tuple[str, str]:
    return await query[tuple[str, str]](t"SELECT current_user::text, session_user::text").one(conn)


async def set_identity(conn: Connection, statement: str, role: str) -> None:
    if statement == "SET ROLE":
        await query_unchecked[tuple[()]](t"SET ROLE {role:ident}").execute(conn)
    else:
        await query_unchecked[tuple[()]](t"SET SESSION AUTHORIZATION {role:ident}").execute(conn)


def pool_options(schema: str, **settings: str) -> ConnectOptions:
    base = ConnectOptions.from_dsn(schema)
    return ConnectOptions.from_dsn(schema, server_settings={**base.server_settings, **settings})


@pytest.mark.parametrize("statement", ["SET ROLE", "SET SESSION AUTHORIZATION"])
@pytest.mark.parametrize("startup_role", [False, True])
def test_a_changed_role_does_not_survive_a_release(
    schema: str, hostile: str, *, statement: str, startup_role: bool
) -> None:
    other = f"{hostile}_b"
    startup = f"{hostile}_a"

    async def run() -> tuple[tuple[str, str], tuple[str, str], tuple[str, str], int, int]:
        options = pool_options(schema, **({"role": startup} if startup_role else {}))
        async with Pool(options, max_size=1) as pool:
            async with pool.acquire() as conn:
                first, pid = await identity(conn), await backend_pid(conn)
                await set_identity(conn, statement, other)
                changed = await identity(conn)
            async with pool.acquire() as conn:
                return first, changed, await identity(conn), pid, await backend_pid(conn)

    first, changed, after, pid, after_pid = asyncio.run(run())
    assert pid == after_pid
    assert other in changed
    assert after == first
    if startup_role:
        # the role from the connection settings is the one that comes back
        assert first[0] == startup
    else:
        assert first[0] == first[1]


def test_search_path_does_not_survive_a_release(schema: str, hostile: str) -> None:
    async def run() -> tuple[str, str, int, int]:
        async with Pool(schema, max_size=1) as pool:
            async with pool.acquire() as conn:
                before, pid = await query[str](t"SHOW search_path").one(conn), await backend_pid(conn)
                await query_unchecked[tuple[()]](t"SET search_path = {hostile:ident}, pg_catalog").execute(conn)
            async with pool.acquire() as conn:
                return before, await query[str](t"SHOW search_path").one(conn), pid, await backend_pid(conn)

    before, after, pid, after_pid = asyncio.run(run())
    assert pid == after_pid
    assert after == before


@pytest.mark.parametrize("explicit_transaction", [False, True])
def test_a_temp_table_does_not_survive_a_release(schema: str, *, explicit_transaction: bool) -> None:
    async def run() -> tuple[bool, int, int]:
        async with Pool(schema, max_size=1) as pool:
            async with pool.acquire() as conn:
                pid = await backend_pid(conn)
                if explicit_transaction:
                    await query(t"BEGIN").execute(conn)
                await query(t"CREATE TEMP TABLE leaked (id int)").execute(conn)
                if explicit_transaction:
                    await query(t"COMMIT").execute(conn)
                assert await query[bool](t"SELECT to_regclass('pg_temp.leaked') IS NOT NULL").one(conn)
            async with pool.acquire() as conn:
                found = await query[bool](t"SELECT to_regclass('pg_temp.leaked') IS NOT NULL").one(conn)
                return found, pid, await backend_pid(conn)

    found, pid, after_pid = asyncio.run(run())
    assert pid == after_pid
    assert found is False


@pytest.mark.parametrize("hooked", [False, True])
def test_an_advisory_lock_does_not_survive_a_release_despite_a_shadowing_function(
    schema: str, hostile: str, *, hooked: bool
) -> None:
    async def noop(_conn: Connection) -> None:
        await asyncio.sleep(0)

    async def locks(conn: Connection, pid: int) -> int:
        return await query[int](t"SELECT count(*)::int4 FROM pg_locks WHERE locktype = 'advisory' AND pid = {pid}").one(
            conn
        )

    async def run() -> tuple[int, int, int, int]:
        async with Pool(schema, max_size=1, reset=noop if hooked else None) as pool:
            async with pool.acquire() as conn:
                pid = await backend_pid(conn)
                await query(t"SELECT pg_advisory_lock({ADVISORY_KEY})").execute(conn)
                # `pg_catalog` listed after the schema makes the schema's same-named function win a bare call
                await query_unchecked[tuple[()]](t"SET search_path = {hostile:ident}, pg_catalog").execute(conn)
                held = await locks(conn, pid)
            async with pool.acquire() as conn:
                return held, await locks(conn, pid), pid, await backend_pid(conn)

    held, after, pid, after_pid = asyncio.run(run())
    assert pid == after_pid
    assert (held, after) == (1, 0)


def test_a_custom_placeholder_setting_is_empty_after_a_release(schema: str) -> None:
    async def run() -> tuple[str, int, int]:
        async with Pool(schema, max_size=1) as pool:
            async with pool.acquire() as conn:
                pid = await backend_pid(conn)
                await query(t"SET app.tenant = 'acme'").execute(conn)
                assert await query[str](t"SHOW app.tenant").one(conn) == "acme"
            async with pool.acquire() as conn:
                return await query[str](t"SHOW app.tenant").one(conn), pid, await backend_pid(conn)

    value, pid, after_pid = asyncio.run(run())
    assert pid == after_pid
    assert not value


async def session_state(observer: Connection, pid: int) -> str:
    return await query[str](t"SELECT state FROM pg_stat_activity WHERE pid = {pid}").one(observer)


@pytest.mark.parametrize("reset_query", [RESET_QUERY, "SET work_mem = '7MB';"], ids=["default", "custom"])
def test_a_begin_after_a_carriage_return_comment_is_rolled_back(schema: str, reset_query: str) -> None:
    async def run() -> tuple[str, str, int, int]:
        async with await connect(schema) as observer, Pool(schema, max_size=1, reset_query=reset_query) as pool:
            async with pool.acquire() as conn:
                pid = await backend_pid(conn)
                # the server ends a line comment at a carriage return, so this opens a transaction
                await query_unchecked[tuple[()]](t"-- opens a transaction\rBEGIN").execute(conn)
                during = await session_state(observer, pid)
            async with pool.acquire() as conn:
                return during, await session_state(observer, pid), pid, await backend_pid(conn)

    during, after, pid, after_pid = asyncio.run(run())
    assert during == "idle in transaction"
    assert after == "idle"
    assert pid == after_pid


@pytest.mark.parametrize("reset_query", [RESET_QUERY, "SET work_mem = '7MB';"], ids=["default", "custom"])
def test_the_idle_guard_discards_a_connection_left_in_a_transaction(
    schema: str, hostile: str, reset_query: str
) -> None:
    async def run() -> tuple[str, str, int, int]:
        async with await connect(schema) as observer, Pool(schema, max_size=1, reset_query=reset_query) as pool:
            async with pool.acquire() as conn:
                pid = await backend_pid(conn)
                # a reset that leaves a transaction open, which the "maybe in a transaction" flag does not see.
                # The search path holds a same-named `=` for timestamptz that is always true
                await query_unchecked[tuple[()]](t"SET search_path = {hostile:ident}, pg_catalog").execute(conn)
                await conn._impl.reset("BEGIN;")
                during = await session_state(observer, pid)
            async with pool.acquire() as conn:
                new_pid = await backend_pid(conn)
                return during, await session_state(observer, new_pid), pid, new_pid

    during, after, pid, new_pid = asyncio.run(run())
    assert during == "idle in transaction"
    assert after == "idle"
    assert pid != new_pid


@pytest.mark.parametrize("opener", [";BEGIN", ";;START TRANSACTION", "/**/;BEGIN", "; -- x\nBEGIN"])
@pytest.mark.parametrize("hooked", [False, True])
def test_a_transaction_opened_after_leading_semicolons_does_not_survive_a_release(
    schema: str, opener: str, *, hooked: bool
) -> None:
    seen: list[str | None] = []

    async def hook(conn: Connection) -> None:
        # a transaction-local setting is still visible when the hook runs inside the transaction
        seen.append(await query_unchecked[str | None](t"SELECT current_setting('app.secret', true)").one(conn))

    async def run() -> tuple[str, str | None, int, int]:
        async with (
            await connect(schema) as observer,
            Pool(schema, max_size=1, reset_query=None, reset=hook if hooked else None) as pool,
        ):
            async with pool.acquire() as conn:
                pid = await backend_pid(conn)
                await query_unchecked[tuple[()]](Template(opener)).execute(conn)
                await query_unchecked[tuple[()]](t"SELECT set_config('app.secret', 'x', true)").execute(conn)
            async with pool.acquire() as conn:
                value = await query_unchecked[str | None](t"SELECT current_setting('app.secret', true)").one(conn)
                return await session_state(observer, pid), value, pid, await backend_pid(conn)

    state, value, pid, after_pid = asyncio.run(run())
    assert state == "idle"
    assert value in {None, ""}
    assert pid == after_pid
    assert all(item in {None, ""} for item in seen)
    assert len(seen) == (2 if hooked else 0)


@pytest.mark.parametrize("reset_query", ["DISCARD ALL", "VACUUM"])
@pytest.mark.parametrize("open_transaction", [False, True])
def test_a_custom_reset_query_that_cannot_run_in_a_transaction_keeps_the_connection(
    schema: str, reset_query: str, *, open_transaction: bool
) -> None:
    async def run() -> list[int]:
        pids = []
        async with Pool(schema, max_size=1, reset_query=reset_query) as pool:
            for _ in range(3):
                async with pool.acquire() as conn:
                    pids.append(await backend_pid(conn))
                    if open_transaction:
                        await query_unchecked[tuple[()]](t"BEGIN").execute(conn)
        return pids

    assert len(set(asyncio.run(run()))) == 1


def test_a_custom_reset_query_still_discards_a_connection_left_in_a_transaction(schema: str) -> None:
    async def run() -> tuple[int, int]:
        async with Pool(schema, max_size=1, reset_query="DISCARD ALL") as pool:
            async with pool.acquire() as conn:
                pid = await backend_pid(conn)
                await conn._impl.reset("BEGIN;")
            async with pool.acquire() as conn:
                return pid, await backend_pid(conn)

    pid, new_pid = asyncio.run(run())
    assert pid != new_pid


def test_a_reset_hook_runs_on_an_idle_connection_after_an_aborted_transaction(schema: str) -> None:
    async def hook(conn: Connection) -> None:
        await query(t"SET work_mem = '13MB'").execute(conn)

    async def run() -> tuple[str, int, int]:
        async with Pool(schema, max_size=1, reset_query=None, reset=hook) as pool:
            async with pool.acquire() as conn:
                pid = await backend_pid(conn)
                await query(t"BEGIN").execute(conn)
                with pytest.raises(DatabaseError):
                    await query[int](t"SELECT 1 / 0").one(conn)
            async with pool.acquire() as conn:
                return await query[str](t"SHOW work_mem").one(conn), pid, await backend_pid(conn)

    work_mem, pid, after_pid = asyncio.run(run())
    assert pid == after_pid
    assert work_mem == "13MB"

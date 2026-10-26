import asyncio
from typing import TYPE_CHECKING, Final

import pytest

from rowx import query
from rowx.errors import ConnectionBusyError, DatabaseError, InterfaceError
from rowx.postgres import Connection, connect

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

pytestmark = [pytest.mark.integration, pytest.mark.db]

POLL_LIMIT: Final[int] = 2000
ADVISORY_KEY: Final[int] = 727_001


def run_with_two[T](dsn: str, body: Callable[[Connection, Connection], Awaitable[T]]) -> T:
    # the second connection observes the first through the catalog
    async def run() -> T:
        async with await connect(dsn) as conn, await connect(dsn) as observer:
            return await body(conn, observer)

    return asyncio.run(run())


async def backend_pid(conn: Connection) -> int:
    return await query[int](t"SELECT pg_backend_pid()").one(conn)


async def activity(observer: Connection, pid: int) -> tuple[str, str]:
    return await query[tuple[str, str]](t"SELECT state, query FROM pg_stat_activity WHERE pid = {pid}").one(observer)


def test_ping_works(schema: str) -> None:
    async def body(conn: Connection, _observer: Connection) -> None:
        await conn.ping()
        await conn.ping()
        assert await query[int](t"SELECT 1").one(conn) == 1

    run_with_two(schema, body)


def test_ping_on_a_closed_connection_is_an_interface_error(schema: str) -> None:
    async def run() -> None:
        conn = await connect(schema)
        await conn.close()
        await conn.ping()

    with pytest.raises(InterfaceError):
        asyncio.run(run())


@pytest.mark.timing
def test_ping_on_a_busy_connection_raises_busy(schema: str) -> None:
    async def body(conn: Connection, observer: Connection) -> None:
        pid = await backend_pid(conn)
        slow = asyncio.create_task(query(t"SELECT pg_sleep(1)").execute(conn))
        for _ in range(POLL_LIMIT):
            state, _ = await activity(observer, pid)
            if state == "active":
                break
        else:
            pytest.fail("the sleeping query never became active")
        with pytest.raises(ConnectionBusyError):
            await conn.ping()
        await slow
        await conn.ping()

    run_with_two(schema, body)


def test_reset_rolls_back_an_open_transaction_from_a_raw_begin(schema: str) -> None:
    async def body(conn: Connection, observer: Connection) -> tuple[str, str, bool]:
        pid = await backend_pid(conn)
        await query(t"BEGIN").execute(conn)
        await query(t"CREATE TEMP TABLE rolled_back (id int)").execute(conn)
        during, _ = await activity(observer, pid)
        await conn.reset()
        after, last = await activity(observer, pid)
        exists = await query[bool](t"SELECT to_regclass('pg_temp.rolled_back') IS NOT NULL").one(conn)
        assert last.startswith("ROLLBACK;")
        return during, after, exists

    assert run_with_two(schema, body) == ("idle in transaction", "idle", False)


def test_reset_rolls_back_a_failed_transaction(schema: str) -> None:
    async def body(conn: Connection, observer: Connection) -> tuple[str, str, int]:
        pid = await backend_pid(conn)
        await query(t"BEGIN").execute(conn)
        with pytest.raises(DatabaseError):
            await query[int](t"SELECT 1 / 0").one(conn)
        during, _ = await activity(observer, pid)
        await conn.reset()
        after, _ = await activity(observer, pid)
        return during, after, await query[int](t"SELECT 1").one(conn)

    assert run_with_two(schema, body) == ("idle in transaction (aborted)", "idle", 1)


def test_reset_rolls_back_a_savepoint_started_transaction(schema: str) -> None:
    async def body(conn: Connection, observer: Connection) -> str:
        pid = await backend_pid(conn)
        await query(t"START TRANSACTION").execute(conn)
        await conn.reset()
        state, _ = await activity(observer, pid)
        return state

    assert run_with_two(schema, body) == "idle"


def test_reset_sends_no_rollback_when_nothing_is_open(schema: str) -> None:
    # a ROLLBACK outside a transaction makes the server log WARNING 25P01, so the statement text
    # the server saw last is the proof that none was sent
    async def body(conn: Connection, observer: Connection) -> tuple[str, str]:
        pid = await backend_pid(conn)
        await conn.reset()
        await conn.reset()
        state, last = await activity(observer, pid)
        return state, last

    state, last = run_with_two(schema, body)
    assert state == "idle"
    assert last.startswith("SELECT pg_advisory_unlock_all()")
    assert "ROLLBACK" not in last


def test_reset_clears_the_flag_after_a_rollback(schema: str) -> None:
    async def body(conn: Connection, observer: Connection) -> str:
        pid = await backend_pid(conn)
        await query(t"BEGIN").execute(conn)
        await conn.reset()
        await conn.reset()
        _, last = await activity(observer, pid)
        return last

    assert "ROLLBACK" not in run_with_two(schema, body)


def test_reset_releases_advisory_locks(schema: str) -> None:
    async def body(conn: Connection, observer: Connection) -> tuple[int, int]:
        pid = await backend_pid(conn)
        count = query[int](t"SELECT count(*)::int4 FROM pg_locks WHERE locktype = 'advisory' AND pid = {pid}")
        await query(t"SELECT pg_advisory_lock({ADVISORY_KEY})").execute(conn)
        held = await count.one(observer)
        await conn.reset()
        return held, await count.one(observer)

    assert run_with_two(schema, body) == (1, 0)


def test_reset_clears_listen_registrations_and_sets(schema: str) -> None:
    async def body(conn: Connection, _observer: Connection) -> tuple[int, int, str, str]:
        listening = query[int](t"SELECT count(*)::int4 FROM pg_listening_channels()")
        original = await query[str](t"SHOW work_mem").one(conn)
        await query(t"LISTEN rowx_reset_test").execute(conn)
        await query(t"SET work_mem = '11MB'").execute(conn)
        listened, changed = await listening.one(conn), await query[str](t"SHOW work_mem").one(conn)
        await conn.reset()
        assert changed == "11MB"
        return listened, await listening.one(conn), original, await query[str](t"SHOW work_mem").one(conn)

    listened, after, original, restored = run_with_two(schema, body)
    assert (listened, after) == (1, 0)
    assert restored == original


def test_reset_leaves_prepared_statements_unchanged(schema: str) -> None:
    async def body(conn: Connection, _observer: Connection) -> tuple[int, int]:
        count = query[int](t"SELECT count(*)::int4 FROM pg_prepared_statements")
        await query[int](t"SELECT 1").one(conn)
        before = await count.one(conn)
        await conn.reset()
        return before, await count.one(conn)

    before, after = run_with_two(schema, body)
    assert before > 0
    assert before == after

import asyncio
from typing import TYPE_CHECKING, Final

import pytest

from rowx import query
from rowx.errors import ConnectionBusyError, DatabaseError, InterfaceError
from rowx.postgres import Connection, connect

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

pytestmark = [pytest.mark.integration, pytest.mark.db]

POLL_LIMIT: Final[int] = 5000


class BoomError(Exception):
    pass


def run_with_two[T](dsn: str, body: Callable[[Connection, Connection], Awaitable[T]]) -> T:
    async def run() -> T:
        async with await connect(dsn) as conn, await connect(dsn) as observer:
            return await body(conn, observer)

    return asyncio.run(run())


async def backend_pid(conn: Connection) -> int:
    return await query[int](t"SELECT pg_backend_pid()").one(conn)


async def wait_for_state(observer: Connection, pid: int, wanted: str) -> str:
    state = ""
    for _ in range(POLL_LIMIT):
        state = await query[str](t"SELECT coalesce(state, '') FROM pg_stat_activity WHERE pid = {pid}").one(observer)
        if state == wanted:
            return state
        await asyncio.sleep(0.001)
    return state


async def count_users(conn: Connection, name: str) -> int:
    return await query[int](t"SELECT count(*) FROM users WHERE name = {name}").one(conn)


def test_a_commit_is_visible_from_a_second_connection(schema: str) -> None:
    async def body(conn: Connection, observer: Connection) -> tuple[int, int]:
        async with conn.transaction() as tx:
            await query(t"INSERT INTO users (name) VALUES ('committed')").execute(tx)
            inside = await count_users(observer, "committed")
        return inside, await count_users(observer, "committed")

    assert run_with_two(schema, body) == (0, 1)


def test_an_exception_rolls_back(schema: str) -> None:
    async def body(conn: Connection, observer: Connection) -> int:
        async def work() -> None:
            async with conn.transaction() as tx:
                await query(t"INSERT INTO users (name) VALUES ('rolled_back')").execute(tx)
                raise BoomError

        with pytest.raises(BoomError):
            await work()
        assert await query[int](t"SELECT 1").one(conn) == 1
        return await count_users(observer, "rolled_back")

    assert run_with_two(schema, body) == 0


def test_rolling_back_a_savepoint_keeps_the_outer_work(schema: str) -> None:
    async def body(conn: Connection, observer: Connection) -> tuple[int, int, int]:
        async with conn.transaction() as tx:
            await query(t"INSERT INTO users (name) VALUES ('outer')").execute(tx)

            async def inner() -> None:
                async with tx.transaction() as sp:
                    await query(t"INSERT INTO users (name) VALUES ('inner')").execute(sp)
                    raise BoomError

            with pytest.raises(BoomError):
                await inner()
            async with tx.transaction() as sp:
                await query(t"INSERT INTO users (name) VALUES ('released')").execute(sp)
        return (
            await count_users(observer, "outer"),
            await count_users(observer, "inner"),
            await count_users(observer, "released"),
        )

    assert run_with_two(schema, body) == (1, 0, 1)


def test_a_swallowed_error_followed_by_a_clean_exit_raises(schema: str) -> None:
    async def body(conn: Connection, observer: Connection) -> int:
        async def work() -> None:
            async with conn.transaction() as tx:
                await query(t"INSERT INTO users (name) VALUES ('swallowed')").execute(tx)
                with pytest.raises(DatabaseError):
                    await query(t"SELECT 1 / 0").execute(tx)

        with pytest.raises(InterfaceError):
            await work()
        assert await query[int](t"SELECT 1").one(conn) == 1
        return await count_users(observer, "swallowed")

    assert run_with_two(schema, body) == 0


def test_a_savepoint_rollback_clears_the_failed_flag(schema: str) -> None:
    async def body(conn: Connection, observer: Connection) -> int:
        async with conn.transaction() as tx:
            await query(t"INSERT INTO users (name) VALUES ('survives')").execute(tx)
            with pytest.raises(DatabaseError):
                async with tx.transaction() as sp:
                    await query(t"SELECT 1 / 0").execute(sp)
            assert await query[int](t"SELECT 1").one(tx) == 1
        return await count_users(observer, "survives")

    assert run_with_two(schema, body) == 1


def test_isolation_and_readonly_are_applied(schema: str) -> None:
    async def body(conn: Connection, _observer: Connection) -> tuple[str, str, str, str]:
        async with conn.transaction(isolation="serializable", readonly=True, deferrable=True) as tx:
            level = await query[str](t"SHOW transaction_isolation").one(tx)
            readonly = await query[str](t"SHOW transaction_read_only").one(tx)
        async with conn.transaction() as tx:
            default_level = await query[str](t"SHOW transaction_isolation").one(tx)
            default_readonly = await query[str](t"SHOW transaction_read_only").one(tx)
        return level, readonly, default_level, default_readonly

    assert run_with_two(schema, body) == ("serializable", "on", "read committed", "off")


def test_an_unknown_isolation_level_is_an_interface_error(schema: str) -> None:
    async def body(conn: Connection, _observer: Connection) -> None:
        with pytest.raises(InterfaceError):
            async with conn.transaction(isolation="chaos"):
                pass
        assert await query[int](t"SELECT 1").one(conn) == 1

    run_with_two(schema, body)


def test_a_serialization_failure_on_commit_raises_and_the_connection_survives(schema: str) -> None:
    async def body(conn: Connection, observer: Connection) -> int:
        await query(t"CREATE TABLE ser (k int PRIMARY KEY, v int)").execute(conn)
        await query(t"INSERT INTO ser VALUES (1, 0), (2, 0)").execute(conn)
        async with (
            observer.transaction(isolation="serializable") as first,
            conn.transaction(isolation="serializable") as second,
        ):
            await query[int](t"SELECT sum(v)::int FROM ser WHERE k = 1").one(first)
            await query[int](t"SELECT sum(v)::int FROM ser WHERE k = 2").one(second)
            await query(t"UPDATE ser SET v = 1 WHERE k = 2").execute(first)
            await query(t"UPDATE ser SET v = 1 WHERE k = 1").execute(second)
            await first.commit()
            with pytest.raises(DatabaseError) as info:
                await second.commit()
            assert info.value.sqlstate == "40001"
        return await query[int](t"SELECT sum(v)::int FROM ser").one(conn)

    assert run_with_two(schema, body) == 1


def test_cancelling_inside_the_block_rolls_back(schema: str) -> None:
    async def body(conn: Connection, observer: Connection) -> tuple[int, int]:
        pid = await backend_pid(conn)

        async def work() -> None:
            async with conn.transaction() as tx:
                await query(t"INSERT INTO users (name) VALUES ('cancelled')").execute(tx)
                await query(t"SELECT pg_sleep(30)").execute(tx)

        task = asyncio.create_task(work())
        for _ in range(POLL_LIMIT):
            active = await query[bool](
                t"SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid = {pid} AND query LIKE '%pg_sleep(30)%' AND state = 'active')"
            ).one(observer)
            if active:
                break
            await asyncio.sleep(0.001)
        else:
            pytest.fail("the sleeping statement never became active")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await wait_for_state(observer, pid, "idle")
        return await query[int](t"SELECT 1").one(conn), await count_users(observer, "cancelled")

    assert run_with_two(schema, body) == (1, 0)


def test_a_second_cancel_during_aexit_still_ends_the_transaction(schema: str) -> None:
    async def body(conn: Connection, observer: Connection) -> tuple[str, int]:
        pid = await backend_pid(conn)

        async def work() -> None:
            async with conn.transaction() as tx:
                await query(t"INSERT INTO users (name) VALUES ('double')").execute(tx)
                await query(t"SELECT pg_sleep(30)").execute(tx)

        task = asyncio.create_task(work())
        for _ in range(POLL_LIMIT):
            active = await query[bool](
                t"SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid = {pid} AND query LIKE '%pg_sleep(30)%' AND state = 'active')"
            ).one(observer)
            if active:
                break
            await asyncio.sleep(0.001)
        else:
            pytest.fail("the sleeping statement never became active")
        task.cancel()
        await asyncio.sleep(0)
        # lands while the transaction is being ended
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        state = await wait_for_state(observer, pid, "idle")
        return state, await count_users(observer, "double")

    assert run_with_two(schema, body) == ("idle", 0)


def test_a_timeout_during_the_commit_discards_the_connection(schema: str) -> None:
    async def body(conn: Connection, observer: Connection) -> None:
        await query(t"CREATE TABLE slow (id int)").execute(conn)
        await query(
            t"CREATE FUNCTION slow_commit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(1); RETURN NULL; END $$"
        ).execute(conn)
        await query(
            t"CREATE CONSTRAINT TRIGGER slow_commit AFTER INSERT ON slow DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION slow_commit()"
        ).execute(conn)
        pid = await backend_pid(conn)

        async def work() -> None:
            async with conn.transaction() as tx:
                await query(t"INSERT INTO slow VALUES (1)").execute(tx)

        task = asyncio.create_task(work())
        for _ in range(POLL_LIMIT):
            active = await query[bool](
                t"SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid = {pid} AND query = 'COMMIT' AND state = 'active')"
            ).one(observer)
            if active:
                break
            await asyncio.sleep(0.001)
        else:
            pytest.fail("the commit never became active")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises((InterfaceError, ConnectionBusyError)):
            await query[int](t"SELECT 1").one(conn)

    run_with_two(schema, body)


def test_a_transaction_cannot_be_used_after_it_ends(schema: str) -> None:
    async def body(conn: Connection, _observer: Connection) -> None:
        async with conn.transaction() as tx:
            await tx.commit()
            with pytest.raises(InterfaceError):
                await query(t"SELECT 1").execute(tx)
            with pytest.raises(InterfaceError):
                await tx.rollback()
        assert tx.closed

        async def reenter() -> None:
            async with tx:
                pass

        with pytest.raises(InterfaceError):
            await reenter()
        assert await query[int](t"SELECT 1").one(conn) == 1

    run_with_two(schema, body)


def test_isolation_arguments_on_a_savepoint_are_an_interface_error(schema: str) -> None:
    async def body(conn: Connection, _observer: Connection) -> None:
        async with conn.transaction() as tx:
            with pytest.raises(InterfaceError):
                tx.transaction(isolation="serializable")
            with pytest.raises(InterfaceError):
                tx.transaction(readonly=True)

    run_with_two(schema, body)


def test_ending_the_outer_transaction_with_a_savepoint_open_is_an_interface_error(schema: str) -> None:
    async def body(conn: Connection, observer: Connection) -> int:
        async with conn.transaction() as tx:
            await query(t"INSERT INTO users (name) VALUES ('open_savepoint')").execute(tx)
            async with tx.transaction() as sp:
                with pytest.raises(InterfaceError):
                    await tx.commit()
                await sp.rollback()
            await tx.rollback()
        return await count_users(observer, "open_savepoint")

    assert run_with_two(schema, body) == 0


def test_a_second_transaction_on_a_busy_connection_is_an_interface_error(schema: str) -> None:
    async def body(conn: Connection, _observer: Connection) -> None:
        async with conn.transaction():
            with pytest.raises(InterfaceError):
                async with conn.transaction():
                    pass
        assert await query[int](t"SELECT 1").one(conn) == 1

    run_with_two(schema, body)

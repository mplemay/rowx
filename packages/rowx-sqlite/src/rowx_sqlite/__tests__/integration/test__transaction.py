import asyncio
import time
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from rowx_core.errors import DatabaseError, InterfaceError
from rowx_sqlite import Connection, ConnectOptions, connect

from rowx import query

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from rowx_sqlite import Transaction

    from rowx import Executor


@pytest_asyncio.fixture
async def other(tmp_path: Path) -> AsyncIterator[Connection]:
    # a second connection to the same file, which waits only briefly for a lock
    second = await connect(ConnectOptions(path=str(tmp_path / "test.db"), mode="rwc", connect_timeout=0.05))
    try:
        yield second
    finally:
        await second.close()


async def enter(transaction: Transaction) -> None:
    async with transaction:
        pass


async def make_table(conn: Connection) -> None:
    await query(t"CREATE TABLE t (n INTEGER)").execute(conn)


async def count(conn: Executor) -> int:
    return await query[int](t"SELECT count(*) FROM t").one(conn)


@pytest.mark.asyncio
async def test_a_committed_transaction_is_visible_and_a_rolled_back_one_is_not(
    conn: Connection,
    other: Connection,
) -> None:
    await make_table(conn)
    async with conn.transaction() as tx:
        await query(t"INSERT INTO t VALUES (1)").execute(tx)
        assert await count(tx) == 1
        assert await count(other) == 0
    assert await count(other) == 1

    async def boom() -> None:
        async with conn.transaction() as tx:
            await query(t"INSERT INTO t VALUES (2)").execute(tx)
            msg = "boom"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="boom"):
        await boom()
    assert await count(conn) == 1

    async with conn.transaction() as tx:
        await query(t"INSERT INTO t VALUES (3)").execute(tx)
        await tx.rollback()
    assert await count(conn) == 1


@pytest.mark.asyncio
async def test_the_default_mode_is_immediate_so_a_second_writer_is_refused_at_once(
    conn: Connection,
    other: Connection,
) -> None:
    await make_table(conn)
    async with conn.transaction():
        # the write lock is taken by BEGIN, before any statement of the first transaction writes
        with pytest.raises(DatabaseError) as info:
            await enter(other.transaction())
        assert info.value.code == "SQLITE_BUSY"
        assert info.value.sqlstate == "55P03"
        # a reader is not blocked in WAL
        assert await count(other) == 0


@pytest.mark.asyncio
async def test_a_deferred_transaction_takes_no_lock_until_it_writes(conn: Connection, other: Connection) -> None:
    await make_table(conn)
    async with conn.transaction(mode="deferred") as tx:
        assert await count(tx) == 0
        # nothing is locked yet, so another connection can write
        await query(t"INSERT INTO t VALUES (1)").execute(other)
    assert await count(conn) == 1


@pytest.mark.asyncio
async def test_a_deferred_transaction_that_upgrades_after_another_write_fails_at_once(
    conn: Connection,
    other: Connection,
) -> None:
    await make_table(conn)

    async def upgrade() -> None:
        async with conn.transaction(mode="deferred") as tx:
            assert await count(tx) == 0
            await query(t"INSERT INTO t VALUES (1)").execute(other)
            await query(t"INSERT INTO t VALUES (2)").execute(tx)

    with pytest.raises(DatabaseError) as info:
        await upgrade()
    assert info.value.code == "SQLITE_BUSY_SNAPSHOT"
    assert info.value.sqlstate == "40001"
    assert await count(conn) == 1


@pytest.mark.asyncio
async def test_exclusive_is_accepted_and_blocks_a_second_writer_but_not_a_reader(
    conn: Connection,
    other: Connection,
) -> None:
    await make_table(conn)
    async with conn.transaction(mode="exclusive") as tx:
        await query(t"INSERT INTO t VALUES (1)").execute(tx)
        with pytest.raises(DatabaseError) as info:
            await query(t"INSERT INTO t VALUES (2)").execute(other)
        assert info.value.code == "SQLITE_BUSY"
        assert await count(other) == 0
    assert await count(other) == 1


@pytest.mark.asyncio
async def test_a_read_only_transaction_refuses_writes_and_restores_the_connection(conn: Connection) -> None:
    await make_table(conn)
    async with conn.transaction(read_only=True) as tx:
        assert await count(tx) == 0
        with pytest.raises(DatabaseError) as info:
            await query(t"INSERT INTO t VALUES (1)").execute(tx)
        assert info.value.sqlstate == "25006"
        assert info.value.code == "SQLITE_READONLY"
        tx._failed = False
    # the write that failed was the caller's to handle; the connection takes writes again
    await query(t"INSERT INTO t VALUES (1)").execute(conn)
    assert await count(conn) == 1


@pytest.mark.asyncio
async def test_a_read_only_transaction_keeps_a_read_only_connection_read_only(tmp_path: Path) -> None:
    path = str(tmp_path / "ro.db")
    writer = await connect(ConnectOptions(path=path, mode="rwc"))
    await make_table(writer)
    await writer.close()
    conn = await connect(ConnectOptions(path=path, read_only=True))
    try:
        async with conn.transaction(read_only=True) as tx:
            assert await count(tx) == 0
        with pytest.raises(DatabaseError) as info:
            await query(t"INSERT INTO t VALUES (1)").execute(conn)
        assert info.value.code == "SQLITE_READONLY"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_a_read_only_connection_takes_a_default_transaction_deferred(tmp_path: Path) -> None:
    path = str(tmp_path / "ro.db")
    writer = await connect(ConnectOptions(path=path, mode="rwc"))
    await make_table(writer)
    await writer.close()
    conn = await connect(ConnectOptions(path=path, read_only=True))
    try:
        async with conn.transaction() as tx:
            assert await count(tx) == 0
    finally:
        await conn.close()
    conn = await connect(ConnectOptions(path=path, mode="ro"))
    try:
        async with conn.transaction() as tx:
            assert await count(tx) == 0
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_read_only_transactions_take_no_mode(conn: Connection) -> None:
    with pytest.raises(InterfaceError, match="read-only"):
        await enter(conn.transaction(mode="immediate", read_only=True))
    async with conn.transaction(mode="deferred", read_only=True):
        pass


@pytest.mark.asyncio
async def test_an_unknown_mode_is_an_interface_error(conn: Connection) -> None:
    with pytest.raises(InterfaceError, match="mode must be"):
        await enter(conn.transaction(mode="bogus"))  # ty: ignore[invalid-argument-type]
    # nothing began
    assert await query[int](t"SELECT 1").one(conn) == 1


@pytest.mark.asyncio
async def test_a_transaction_cannot_begin_inside_another(conn: Connection) -> None:
    async with conn.transaction():
        with pytest.raises(InterfaceError, match="already in a transaction"):
            await enter(conn.transaction())


@pytest.mark.asyncio
async def test_savepoints_nest_and_roll_back_alone(conn: Connection) -> None:
    await make_table(conn)
    async with conn.transaction() as tx:
        await query(t"INSERT INTO t VALUES (1)").execute(tx)

        async def inner_fails() -> None:
            async with tx.transaction() as inner:
                await query(t"INSERT INTO t VALUES (2)").execute(inner)
                async with inner.transaction() as innermost:
                    await query(t"INSERT INTO t VALUES (3)").execute(innermost)
                assert await count(inner) == 3
                msg = "inner"
                raise RuntimeError(msg)

        with pytest.raises(RuntimeError, match="inner"):
            await inner_fails()
        assert await count(tx) == 1
        async with tx.transaction() as again:
            await query(t"INSERT INTO t VALUES (4)").execute(again)
    assert await query[int](t"SELECT n FROM t ORDER BY n").all(conn) == [1, 4]


@pytest.mark.asyncio
async def test_a_savepoint_takes_no_mode(conn: Connection) -> None:
    async with conn.transaction() as tx:
        with pytest.raises(InterfaceError, match="savepoint takes no"):
            tx.transaction(mode="deferred")
        with pytest.raises(InterfaceError, match="savepoint takes no"):
            tx.transaction(read_only=True)


@pytest.mark.asyncio
async def test_a_savepoint_needs_an_open_transaction(conn: Connection) -> None:
    with pytest.raises(InterfaceError, match="no open transaction"):
        await conn._impl._savepoint()


@pytest.mark.asyncio
async def test_rollback_to_leaves_the_savepoint_open_until_it_is_released(conn: Connection) -> None:
    await make_table(conn)
    impl = conn._impl
    await impl._begin(None, read_only=False)
    try:
        n = await impl._savepoint()
        await impl._execute("INSERT INTO t VALUES (1)", [])
        await impl._rollback_to_savepoint(n)
        # still open: it can be rolled back to again, and released
        await impl._execute("INSERT INTO t VALUES (2)", [])
        await impl._rollback_to_savepoint(n)
        await impl._release_savepoint(n)
        # now it is gone
        with pytest.raises(DatabaseError, match="no such savepoint"):
            await impl._rollback_to_savepoint(n)
    finally:
        await impl._end_transaction(commit=False)
    assert await count(conn) == 0


@pytest.mark.asyncio
async def test_a_swallowed_error_rolls_the_transaction_back_instead_of_committing(conn: Connection) -> None:
    await make_table(conn)

    async def swallow() -> None:
        async with conn.transaction() as tx:
            await query(t"INSERT INTO t VALUES (1)").execute(tx)
            with pytest.raises(DatabaseError):
                await query(t"INSERT INTO missing VALUES (1)").execute(tx)

    with pytest.raises(InterfaceError, match="swallowed"):
        await swallow()
    assert await count(conn) == 0


@pytest.mark.asyncio
async def test_a_failed_commit_ends_the_transaction(conn: Connection) -> None:
    await query(t"CREATE TABLE p (id INTEGER PRIMARY KEY)").execute(conn)
    await query(t"CREATE TABLE c (p INTEGER REFERENCES p (id) DEFERRABLE INITIALLY DEFERRED)").execute(conn)
    with pytest.raises(DatabaseError) as info:
        async with conn.transaction() as tx:
            # the violation is only checked by COMMIT
            await query(t"INSERT INTO c VALUES (1)").execute(tx)
    assert info.value.code == "SQLITE_CONSTRAINT_FOREIGNKEY"
    # SQLite would leave the transaction open; the connection is back in autocommit
    await query(t"INSERT INTO p VALUES (1)").execute(conn)
    assert await query[int](t"SELECT count(*) FROM c").one(conn) == 0
    async with conn.transaction():
        pass


LONG_WRITE = query(
    t"INSERT INTO t WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 1000000000000) SELECT n FROM c",
)


@pytest.mark.asyncio
async def test_an_interrupted_write_in_a_transaction_aborts_it_like_postgresql(tmp_path: Path) -> None:
    conn = await connect(ConnectOptions(path=str(tmp_path / "abort.db"), mode="rwc", statement_timeout=0.3))
    try:
        await make_table(conn)

        async def body() -> None:
            async with conn.transaction() as tx:
                await query(t"INSERT INTO t VALUES (0)").execute(tx)
                with pytest.raises(DatabaseError) as timed_out:
                    await LONG_WRITE.execute(tx)
                assert timed_out.value.sqlstate == "57014"
                # SQLite rolled the whole transaction back, so what follows is what PostgreSQL raises for an
                # aborted transaction
                with pytest.raises(DatabaseError) as aborted:
                    await query[int](t"SELECT count(*) FROM t").one(tx)
                assert aborted.value.sqlstate == "25P02"
                with pytest.raises(DatabaseError) as again:
                    await query(t"INSERT INTO t VALUES (1)").execute(tx)
                assert again.value.sqlstate == "25P02"
                with pytest.raises(DatabaseError) as stream:
                    await anext(query[int](t"SELECT n FROM t").fetch(tx))
                assert stream.value.sqlstate == "25P02"
                with pytest.raises(DatabaseError) as nested:
                    await enter(tx.transaction())
                assert nested.value.sqlstate == "25P02"

        with pytest.raises(InterfaceError, match="swallowed"):
            await body()
        # the connection is back in autocommit and usable, and nothing of the transaction survived
        assert await count(conn) == 0
        await query(t"INSERT INTO t VALUES (1)").execute(conn)
        assert await count(conn) == 1
        async with conn.transaction() as tx:
            await query(t"INSERT INTO t VALUES (2)").execute(tx)
        assert await count(conn) == 2
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_a_cancelled_write_in_a_transaction_aborts_it_and_the_connection_survives(conn: Connection) -> None:
    await make_table(conn)
    # rows of a kilobyte, so the write outgrows the page cache and is stepping for certain when the timeout cancels it
    long_write = query(
        t"INSERT INTO t WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 1000000000000) SELECT length(zeroblob(1000)) FROM c",
    )

    async def body() -> None:
        async with conn.transaction() as tx:
            await query(t"INSERT INTO t VALUES (0)").execute(tx)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(long_write.execute(tx), timeout=0.5)
            with pytest.raises(DatabaseError) as aborted:
                await query[int](t"SELECT count(*) FROM t").one(tx)
            assert aborted.value.sqlstate == "25P02"

    with pytest.raises(InterfaceError, match="swallowed"):
        await body()
    assert await count(conn) == 0
    await query(t"INSERT INTO t VALUES (1)").execute(conn)
    assert await count(conn) == 1


@pytest.mark.asyncio
async def test_an_interrupted_read_in_a_transaction_leaves_it_open(tmp_path: Path) -> None:
    conn = await connect(ConnectOptions(path=str(tmp_path / "read.db"), mode="rwc", statement_timeout=0.2))
    try:
        await make_table(conn)
        async with conn.transaction() as tx:
            await query(t"INSERT INTO t VALUES (1)").execute(tx)
            with pytest.raises(DatabaseError) as timed_out:
                await query[int](
                    t"WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 1000000000000000) SELECT count(*) FROM c",
                ).one(tx)
            assert timed_out.value.sqlstate == "57014"
            # only a write rolls the transaction back: the insert is still there
            assert await count(tx) == 1
            tx._failed = False
        assert await count(conn) == 1
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_a_cancelled_begin_that_waits_for_the_lock_leaves_no_transaction_behind(tmp_path: Path) -> None:
    path = str(tmp_path / "wait.db")
    holder = await connect(ConnectOptions(path=path, mode="rwc"))
    waiter = await connect(ConnectOptions(path=path, connect_timeout=3))
    try:
        await make_table(holder)
        async with holder.transaction():
            # the BEGIN waits in SQLite's busy handler, which an interrupt does not wake
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(enter(waiter.transaction()), 0.2)
        async with waiter.transaction() as tx:
            await query(t"INSERT INTO t VALUES (1)").execute(tx)
        assert await count(waiter) == 1
        await query(t"INSERT INTO t VALUES (2)").execute(holder)
        assert await count(holder) == 2
    finally:
        await holder.close()
        await waiter.close()


# blocks the event loop until the native side has handed the new transaction or savepoint over (its depth rose), so the
# awaiting `__aenter__` cannot resume before the cancel lands: the window where the result is delivered but never seen
def block_until_handed_over(conn: Connection, depth: int) -> None:
    deadline = time.monotonic() + 5
    while conn._impl._txn_depth < depth:
        assert time.monotonic() < deadline, "the transaction was never handed over"


async def cancel_after_hand_over(conn: Connection, transaction: Transaction, depth: int) -> None:
    task = asyncio.ensure_future(AsyncExitStack().enter_async_context(transaction))
    # one turn of the loop, so the task calls the native begin
    await asyncio.sleep(0)
    block_until_handed_over(conn, depth)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_a_begin_cancelled_after_it_was_handed_over_rolls_back(conn: Connection, other: Connection) -> None:
    await make_table(conn)
    await cancel_after_hand_over(conn, conn.transaction(), 1)
    assert conn._impl._txn_depth == 0
    # the connection is back in autocommit: a write is visible elsewhere, and a new transaction begins
    await query(t"INSERT INTO t VALUES (1)").execute(conn)
    assert await count(other) == 1
    async with conn.transaction() as tx:
        await query(t"INSERT INTO t VALUES (2)").execute(tx)
    assert await count(other) == 2


@pytest.mark.asyncio
async def test_a_read_only_begin_cancelled_after_it_was_handed_over_restores_writes(conn: Connection) -> None:
    await make_table(conn)
    await cancel_after_hand_over(conn, conn.transaction(read_only=True), 1)
    await query(t"INSERT INTO t VALUES (1)").execute(conn)
    assert await count(conn) == 1


@pytest.mark.asyncio
async def test_a_savepoint_cancelled_after_it_was_handed_over_is_released(conn: Connection, other: Connection) -> None:
    await make_table(conn)
    async with conn.transaction() as outer:
        await cancel_after_hand_over(conn, outer.transaction(), 2)
        assert conn._impl._txn_depth == 1
        async with outer.transaction() as inner:
            await query(t"INSERT INTO t VALUES (1)").execute(inner)
    assert await count(other) == 1


@pytest.mark.asyncio
async def test_cancelling_a_begin_at_any_point_leaves_no_transaction_behind(
    conn: Connection, other: Connection
) -> None:
    await make_table(conn)
    for turns in range(200):
        stack = AsyncExitStack()
        task = asyncio.ensure_future(stack.enter_async_context(conn.transaction()))
        for _ in range(turns % 7):
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            await stack.aclose()
        async with conn.transaction():
            pass
    await query(t"INSERT INTO t VALUES (1)").execute(conn)
    assert await count(other) == 1

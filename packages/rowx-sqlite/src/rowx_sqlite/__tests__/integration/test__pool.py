import asyncio
from typing import TYPE_CHECKING, Any, Final

import pytest
import pytest_asyncio
from rowx_core.errors import DatabaseError, InterfaceError, PoolTimeoutError
from rowx_sqlite import ConnectOptions, Pool

from rowx import query

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from rowx_sqlite import Connection
    from rowx_sqlite._core import Connection as ConnectionImpl

UNTIL_ATTEMPTS: Final[int] = 30000
LONG_QUERY = (
    "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 1000000000000000) SELECT count(*) FROM c"
)
# rows of a kilobyte, so the write outgrows the page cache and is stepping for certain when the timeout cancels it
LONG_WRITE = "INSERT INTO t WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 1000000000000) SELECT length(zeroblob(1000)) FROM c"


def file_options(tmp_path: Path, name: str = "pool.db") -> ConnectOptions:
    return ConnectOptions(path=str(tmp_path / name), mode="rwc", connect_timeout=5)


async def make_table(pool: Pool) -> None:
    await query(t"CREATE TABLE IF NOT EXISTS t (n INTEGER)").execute(pool)


async def count(executor: Any) -> int:  # ruff: ignore[any-type]
    return await query[int](t"SELECT count(*) FROM t").one(executor)


# Waits for pool state that a runtime thread changes, so there is nothing to await on. It returns when the condition
# holds, and the bound only keeps a broken pool from hanging the test.
async def until(condition: Callable[[], bool]) -> None:
    for _ in range(UNTIL_ATTEMPTS):
        if condition():
            return
        await asyncio.sleep(0.001)
    msg = "the condition never held"
    raise AssertionError(msg)


async def enter(pool: Pool) -> None:
    async with pool.transaction():
        pass


async def fail_in_transaction(pool: Pool, msg: str) -> None:
    async with pool.transaction() as tx:
        await query(t"INSERT INTO t VALUES (1)").execute(tx)
        raise RuntimeError(msg)


async def write_in_read_only_transaction(pool: Pool, msg: str) -> None:
    async with pool.transaction(read_only=True) as tx:
        assert await count(tx) == 0
        with pytest.raises(DatabaseError, match="readonly"):
            await query(t"INSERT INTO t VALUES (1)").execute(tx)
        raise RuntimeError(msg)


# the SQL is built at run time, so this goes to the native connection that the query objects call; the positional
# flags are its fixed argument list
async def native_fetch(impl: ConnectionImpl, sql: str) -> list[object]:
    return await impl._fetch(sql, [], None, False, True, None, None)  # ruff: ignore[boolean-positional-value-in-call]


async def noop(conn: Connection) -> None:
    pass


# the options


@pytest.mark.asyncio
async def test_a_file_pool_takes_the_postgresql_defaults(tmp_path: Path) -> None:
    pool = Pool(file_options(tmp_path))
    assert (pool.min_size, pool.max_size) == (0, 10)
    assert (pool.size, pool.idle, pool.waiting, pool.closed) == (0, 0, 0, False)
    await pool.close()
    assert pool.closed


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"max_size": 0}, "max_size must be at least 1"),
        ({"min_size": 3, "max_size": 2}, "min_size must be between 0 and max_size"),
        ({"acquire_timeout": -1.0}, "acquire_timeout must not be negative"),
        ({"idle_timeout": -1.0}, "idle_timeout must not be negative"),
        ({"max_queries": 0}, "max_queries must be at least 1"),
    ],
)
def test_a_file_pool_rejects_bad_options(tmp_path: Path, options: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Pool(file_options(tmp_path), **options)


@pytest.mark.asyncio
async def test_a_file_pool_takes_explicit_options(tmp_path: Path) -> None:
    pool = Pool(file_options(tmp_path), min_size=2, max_size=3, idle_timeout=None, max_lifetime=None)
    try:
        assert (pool.min_size, pool.max_size) == (2, 3)
        await pool.open()
        assert pool.size == 2
    finally:
        await pool.close()


# the in-memory rules


@pytest.mark.asyncio
async def test_an_in_memory_pool_keeps_one_connection_created_at_open() -> None:
    pool = Pool("sqlite::memory:")
    try:
        assert (pool.min_size, pool.max_size) == (1, 1)
        assert pool.size == 0
        await pool.open()
        assert (pool.size, pool.idle) == (1, 1)
    finally:
        await pool.close()


@pytest.mark.parametrize(
    "options",
    [
        {"min_size": 0},
        {"max_size": 2},
        {"idle_timeout": 600.0},
        {"max_lifetime": 1.0},
        {"max_queries": 5},
    ],
    ids=lambda options: next(iter(options)),
)
def test_an_in_memory_pool_rejects_an_option_that_contradicts_it(options: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=f"{next(iter(options))} must be"):
        Pool("sqlite::memory:", **options)


@pytest.mark.asyncio
async def test_an_in_memory_pool_accepts_the_options_it_resolves_to() -> None:
    pool = Pool(
        "sqlite::memory:",
        min_size=1,
        max_size=1,
        idle_timeout=None,
        max_lifetime=None,
        max_queries=None,
    )
    try:
        await pool.open()
        assert (pool.min_size, pool.max_size, pool.size) == (1, 1, 1)
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_an_in_memory_pool_is_one_database_across_leases() -> None:
    pool = Pool(ConnectOptions(memory=True))
    try:
        await make_table(pool)
        await query(t"INSERT INTO t VALUES (1)").execute(pool)
        async with pool.acquire() as conn:
            await query(t"INSERT INTO t VALUES (2)").execute(conn)
        async with pool.transaction() as tx:
            await query(t"INSERT INTO t VALUES (3)").execute(tx)
        assert await count(pool) == 3
        assert pool.size == 1
    finally:
        await pool.close()


@pytest.mark.parametrize("memory", [True, False])
def test_shared_cache_is_rejected(tmp_path: Path, *, memory: bool) -> None:
    dsn = "sqlite::memory:?cache=shared" if memory else f"sqlite:///{tmp_path / 'shared.db'}?cache=shared"
    with pytest.raises(InterfaceError, match="cache"):
        Pool(dsn)


@pytest.mark.asyncio
async def test_a_lost_in_memory_connection_breaks_the_pool_for_good() -> None:
    pool = Pool("sqlite::memory:", acquire_timeout=60.0)
    try:
        await make_table(pool)
        await query(t"INSERT INTO t VALUES (1)").execute(pool)
        # the connection is retired at its next checkout, and a new one would be an empty database
        pool.expire_connections()
        for _ in range(2):
            with pytest.raises(InterfaceError, match="permanently broken"):
                await asyncio.wait_for(pool.acquire(), timeout=30)
        with pytest.raises(InterfaceError, match="permanently broken"):
            await asyncio.wait_for(query[int](t"SELECT 1").one(pool), timeout=30)
        with pytest.raises(InterfaceError, match="permanently broken"):
            await asyncio.wait_for(anext(query[int](t"SELECT 1").fetch(pool)), timeout=30)
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_a_detached_in_memory_connection_breaks_the_pool() -> None:
    pool = Pool("sqlite::memory:", acquire_timeout=60.0)
    try:
        conn = await pool.acquire()
        conn.detach()
        with pytest.raises(InterfaceError, match="permanently broken"):
            await asyncio.wait_for(pool.acquire(), timeout=30)
        # the detached connection is the caller's, and still holds the database
        assert await query[int](t"SELECT 1").one(conn) == 1
        await conn.close()
    finally:
        await pool.close()


# leases


@pytest_asyncio.fixture
async def pool(tmp_path: Path) -> AsyncIterator[Pool]:
    opened = Pool(file_options(tmp_path), max_size=4, acquire_timeout=10.0)
    try:
        await make_table(opened)
        yield opened
    finally:
        await opened.close()


@pytest.mark.asyncio
async def test_acquire_times_out_while_every_connection_is_leased(tmp_path: Path) -> None:
    pool = Pool(file_options(tmp_path), max_size=1)
    try:
        async with pool.acquire():
            with pytest.raises(PoolTimeoutError):
                await pool.acquire(timeout=0.1)
            assert pool.waiting == 0
        async with pool.acquire(timeout=5) as conn:
            assert await query[int](t"SELECT 1").one(conn) == 1
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_a_released_connection_is_not_usable(pool: Pool) -> None:
    async with pool.acquire() as conn:
        assert await query[int](t"SELECT 1").one(conn) == 1
    with pytest.raises(InterfaceError, match="released to the pool"):
        await query[int](t"SELECT 1").one(conn)


@pytest.mark.asyncio
async def test_readers_finish_while_a_writer_holds_an_immediate_transaction(pool: Pool) -> None:
    await query(t"INSERT INTO t VALUES (1)").execute(pool)
    async with pool.transaction() as writer:
        await query(t"INSERT INTO t VALUES (2)").execute(writer)

        async def read() -> int:
            async with pool.acquire() as conn:
                return await count(conn)

        # the readers are awaited inside the writer's transaction, so each one finished while it was open
        seen = await asyncio.wait_for(asyncio.gather(*(read() for _ in range(3))), timeout=30)
        assert seen == [1, 1, 1]
        assert await count(writer) == 2
    assert await count(pool) == 2


@pytest.mark.asyncio
async def test_a_second_writer_fails_once_the_busy_timeout_runs_out(tmp_path: Path) -> None:
    pool = Pool(ConnectOptions(path=str(tmp_path / "w.db"), mode="rwc", connect_timeout=0.2), max_size=2)
    try:
        await make_table(pool)
        async with pool.transaction():
            with pytest.raises(DatabaseError, match="locked"):
                await enter(pool)
        async with pool.transaction() as tx:
            await query(t"INSERT INTO t VALUES (1)").execute(tx)
        assert await count(pool) == 1
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_a_cancelled_acquire_leaves_the_pool_consistent(tmp_path: Path) -> None:
    pool = Pool(file_options(tmp_path), max_size=1, acquire_timeout=60.0)
    try:
        held = await pool.acquire()
        waiter = asyncio.ensure_future(pool.acquire())
        await until(lambda: pool.waiting == 1)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        await until(lambda: pool.waiting == 0)
        await held.close()
        async with pool.acquire(timeout=10) as conn:
            assert await query[int](t"SELECT 1").one(conn) == 1
        assert pool.size == 1
    finally:
        await pool.close()


@pytest.mark.parametrize("hooked", [False, True])
@pytest.mark.asyncio
async def test_the_next_lease_is_idle_after_an_interrupted_write(tmp_path: Path, *, hooked: bool) -> None:
    pool = Pool(file_options(tmp_path), max_size=1, reset=noop if hooked else None)
    try:
        await make_table(pool)
        async with pool.acquire() as conn:
            impl = conn._impl
            await impl._begin(None, read_only=False)
            await impl._execute("INSERT INTO t VALUES (0)", [])
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(impl._execute(LONG_WRITE, []), timeout=0.5)
            # SQLite rolled the whole transaction back by itself, so a ROLLBACK now would fail
        async with pool.acquire() as conn:
            assert await count(conn) == 0
            async with conn.transaction() as tx:
                await query(t"INSERT INTO t VALUES (1)").execute(tx)
        assert await count(pool) == 1
        assert pool.size == 1
    finally:
        await pool.close()


@pytest.mark.parametrize("hooked", [False, True])
@pytest.mark.asyncio
async def test_the_next_lease_is_idle_after_an_interrupted_read_in_a_transaction(
    tmp_path: Path,
    *,
    hooked: bool,
) -> None:
    pool = Pool(file_options(tmp_path), max_size=1, reset=noop if hooked else None)
    try:
        await make_table(pool)
        async with pool.acquire() as conn:
            impl = conn._impl
            await impl._begin(None, read_only=False)
            await impl._execute("INSERT INTO t VALUES (0)", [])
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(native_fetch(impl, LONG_QUERY), timeout=0.3)
            # only a write rolls the transaction back, so it is still open when the lease ends
        async with pool.acquire() as conn:
            assert await count(conn) == 0
            async with conn.transaction():
                pass
    finally:
        await pool.close()


@pytest.mark.parametrize("hooked", [False, True])
@pytest.mark.asyncio
async def test_a_stream_kept_past_its_lease_does_not_block_the_release(tmp_path: Path, *, hooked: bool) -> None:
    pool = Pool(file_options(tmp_path), max_size=1, reset=noop if hooked else None)
    try:
        # few enough rows that the worker finishes while the batches are still unread
        async with pool.acquire() as conn:
            kept = query[int](
                t"WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 20) SELECT n FROM c"
            ).fetch(conn, batch_size=2)
            assert await anext(kept) == 1

        async def next_lease() -> int:
            async with pool.acquire(timeout=5) as conn:
                return await query[int](t"SELECT 1").one(conn)

        assert await asyncio.wait_for(next_lease(), timeout=30) == 1
        assert pool.size == 1
        del kept
    finally:
        await asyncio.wait_for(pool.close(), timeout=30)


@pytest.mark.parametrize("hooked", [False, True])
@pytest.mark.asyncio
async def test_a_query_still_running_at_release_does_not_interrupt_the_next_lease(
    tmp_path: Path,
    *,
    hooked: bool,
) -> None:
    pool = Pool(file_options(tmp_path), max_size=1, reset=noop if hooked else None)
    try:
        async with pool.acquire() as conn:
            running = asyncio.ensure_future(native_fetch(conn._impl, LONG_QUERY))
            # the statement is claimed synchronously by the call, so the release has to end it
            await asyncio.sleep(0)
        with pytest.raises(DatabaseError, match="interrupt"):
            await asyncio.wait_for(running, timeout=30)
        await until(lambda: pool.idle == 1)
        async with pool.acquire(timeout=10) as conn:
            assert await query[int](t"SELECT 1").one(conn) == 1
            assert await query[int](t"SELECT 2").all(conn) == [2]
    finally:
        await asyncio.wait_for(pool.close(), timeout=30)


@pytest.mark.asyncio
async def test_a_released_read_only_transaction_leaves_the_connection_writable(tmp_path: Path) -> None:
    pool = Pool(file_options(tmp_path), max_size=1)
    try:
        await make_table(pool)
        async with pool.acquire() as conn:
            await conn._impl._begin(None, read_only=True)
        async with pool.acquire() as conn:
            await query(t"INSERT INTO t VALUES (1)").execute(conn)
        assert await count(pool) == 1
    finally:
        await pool.close()


# transactions taken from the pool


@pytest.mark.asyncio
async def test_a_pool_transaction_commits(pool: Pool) -> None:
    async with pool.transaction() as tx:
        await query(t"INSERT INTO t VALUES (1)").execute(tx)
        async with tx.transaction() as nested:
            await query(t"INSERT INTO t VALUES (2)").execute(nested)
    assert await count(pool) == 2
    assert pool.idle == pool.size


@pytest.mark.asyncio
async def test_a_pool_transaction_rolls_back_on_an_error(pool: Pool) -> None:
    msg = "boom"
    with pytest.raises(RuntimeError, match=msg):
        await fail_in_transaction(pool, msg)
    assert await count(pool) == 0
    assert pool.idle == pool.size


@pytest.mark.asyncio
async def test_a_read_only_pool_transaction_refuses_a_write(pool: Pool) -> None:
    msg = "rolled back"
    with pytest.raises(RuntimeError, match=msg):
        await write_in_read_only_transaction(pool, msg)
    async with pool.transaction(mode="deferred") as tx:
        await query(t"INSERT INTO t VALUES (1)").execute(tx)
    assert await count(pool) == 1


# executing straight on the pool


@pytest.mark.asyncio
async def test_a_pool_runs_queries_and_streams_on_its_own(pool: Pool) -> None:
    for n in range(5):
        await query(t"INSERT INTO t VALUES ({n})").execute(pool)
    assert await query[int](t"SELECT n FROM t ORDER BY n").all(pool) == [0, 1, 2, 3, 4]
    assert [n async for n in query[int](t"SELECT n FROM t ORDER BY n").fetch(pool)] == [0, 1, 2, 3, 4]
    assert pool.idle == pool.size


# hooks


@pytest.mark.asyncio
async def test_hooks_run_for_every_lease(tmp_path: Path) -> None:
    calls: list[str] = []

    async def init(conn: Connection) -> None:
        calls.append("init")
        await query(t"CREATE TABLE IF NOT EXISTS t (n INTEGER)").execute(conn)

    async def setup(conn: Connection) -> None:
        calls.append("setup")
        assert await query[int](t"SELECT 1").one(conn) == 1

    async def reset(conn: Connection) -> None:
        calls.append("reset")
        # an open transaction is already ended, so the hook can use the connection
        assert await count(conn) == 0

    pool = Pool(file_options(tmp_path), max_size=1, init=init, setup=setup, reset=reset)
    try:
        async with pool.acquire() as conn:
            await conn._impl._begin(None, read_only=False)
            await conn._impl._execute("INSERT INTO t VALUES (1)", [])
        async with pool.acquire():
            pass
        assert await count(pool) == 0
        assert calls == ["init", "setup", "reset", "setup", "reset", "setup", "reset"]
    finally:
        await pool.close()

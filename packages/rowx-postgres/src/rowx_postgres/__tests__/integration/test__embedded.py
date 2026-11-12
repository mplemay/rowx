import asyncio
import os
import signal
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
import time
import warnings
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Final
from urllib.parse import parse_qs, quote, urlparse

import pytest
from rowx_core.errors import DatabaseError, InterfaceError, PoolTimeoutError
from rowx_postgres import Connection, Pool, connect, embedded
from rowx_postgres._embedded import EXPECTED_API_VERSION

from rowx import query

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

pytestmark = [pytest.mark.embedded]

TOO_MANY_CONNECTIONS: Final[str] = "53300"
UNDEFINED_TABLE: Final[str] = "42P01"
POLL_LIMIT: Final[int] = 1000
POLL_INTERVAL: Final[float] = 0.01
INTERRUPT_LIMIT: Final[float] = 8.0
CHILD_WAIT: Final[float] = 60.0
SLEEPER: Final[str] = (
    "import asyncio\n"
    "from rowx import query\n"
    "from rowx_postgres import connect\n"
    "async def main():\n"
    "    async with await connect('embedded://') as c:\n"
    "        print('ready', flush=True)\n"
    "        await query(t'SELECT pg_sleep(30)').execute(c)\n"
    "asyncio.run(main())\n"
)


def socket_directory(dsn: str) -> Path:
    return Path(parse_qs(urlparse(dsn).query)["host"][0])


def directory_url(path: Path) -> str:
    return f"embedded://{quote(str(path))}"


def run[T](factory: Callable[[], Coroutine[object, object, T]]) -> T:
    return asyncio.run(factory())


async def until_idle(pool: Pool) -> None:
    # the release resets the connection in the background, so poll with a real sleep, bounded
    for _ in range(POLL_LIMIT):
        if pool.idle == 1:
            return
        await asyncio.sleep(POLL_INTERVAL)
    pytest.fail("the connection never went back to the pool")


async def number(conn: Connection | Pool, value: int) -> int:
    return await query[int](t"SELECT {value}::int4").one(conn)


def test_the_companion_speaks_the_api_version_this_adapter_expects() -> None:
    assert import_module("rowx_embedded").API_VERSION == EXPECTED_API_VERSION


def test_a_memory_connect_round_trips_and_close_closes_the_instance() -> None:
    async def main() -> tuple[Path, bool]:
        conn = await connect("embedded://")
        assert conn._owner is not None
        directory = socket_directory(conn._owner.dsn)
        await query[tuple[()]](t"CREATE TABLE users (id int PRIMARY KEY, name text NOT NULL)").execute(conn)
        await query[tuple[()]](t"INSERT INTO users VALUES (1, 'ada')").execute(conn)
        assert await query[tuple[int, str]](t"SELECT id, name FROM users").one(conn) == (1, "ada")
        existed = directory.exists()
        await conn.close()
        assert conn.closed
        return directory, existed

    directory, existed = run(main)
    assert existed
    assert not directory.exists()


def test_the_context_manager_closes_the_instance() -> None:
    async def main() -> Path:
        async with await connect("embedded://") as conn:
            assert conn._owner is not None
            directory = socket_directory(conn._owner.dsn)
            assert directory.exists()
        return directory

    assert not run(main).exists()


def test_two_memory_connects_are_two_independent_databases() -> None:
    async def main() -> str:
        async with await connect("embedded://") as first, await connect("embedded://") as second:
            await query[tuple[()]](t"CREATE TABLE only_in_first (id int)").execute(first)
            with pytest.raises(DatabaseError) as caught:
                await count_only_in_first(second)
            return caught.value.sqlstate

    assert run(main) == UNDEFINED_TABLE


async def count_only_in_first(conn: Connection) -> int:
    return await query[int](t"SELECT count(*)::int4 FROM only_in_first").one(conn)


def test_a_directory_persists_between_connections(tmp_path: Path) -> None:
    url = directory_url(tmp_path / "db")

    async def main() -> list[tuple[int, str]]:
        async with await connect(url) as conn:
            await query[tuple[()]](t"CREATE TABLE kept (id int PRIMARY KEY, name text)").execute(conn)
            await query[tuple[()]](t"INSERT INTO kept VALUES (1, 'ada'), (2, 'grace')").execute(conn)
        async with await connect(url) as conn:
            return await query[tuple[int, str]](t"SELECT id, name FROM kept ORDER BY id").all(conn)

    assert run(main) == [(1, "ada"), (2, "grace")]


def test_a_directory_has_one_owner(tmp_path: Path) -> None:
    url = directory_url(tmp_path / "db")

    async def main() -> str:
        async with await connect(url):
            with pytest.raises(InterfaceError) as caught:
                await connect(url)
            return str(caught.value)

    assert "is in use by" in run(main)


def test_a_failed_connect_leaves_no_instance_behind(tmp_path: Path) -> None:
    url = f"{directory_url(tmp_path / 'db')}?dbname=nope"

    async def main() -> None:
        with pytest.raises(DatabaseError):
            await connect(url)
        # the directory is free again, so the failed connect closed its instance
        async with await connect(directory_url(tmp_path / "db")) as conn:
            assert await number(conn, 1) == 1

    run(main)


def test_forwarded_parameters_reach_the_session() -> None:
    async def main() -> tuple[str, str]:
        async with await connect("embedded://?application_name=svc&options=-c%20search_path%3Dpg_catalog") as conn:
            name = await query[str](t"SELECT current_setting('application_name')").one(conn)
            path = await query[str](t"SELECT current_setting('search_path')").one(conn)
            return name, path

    assert run(main) == ("svc", "pg_catalog")


def test_an_embedded_pool_reuses_its_one_connection_and_closes_its_instance() -> None:
    async def main() -> tuple[int, int, int, int, bool, Path]:
        pool = Pool("embedded://")
        assert (pool.size, pool.max_size) == (0, 1)
        async with pool.acquire() as conn:
            await query[tuple[()]](t"CREATE TEMP TABLE scratch (id int)").execute(conn)
            await query[tuple[()]](t"INSERT INTO scratch VALUES (1)").execute(conn)
        await until_idle(pool)
        assert (pool.size, pool.idle) == (1, 1)
        assert pool._database is not None
        directory = socket_directory(pool._database.dsn)
        assert directory.exists()
        # the release reset the session, so the temp table is gone, but it is the same instance
        async with pool.acquire() as conn:
            assert await number(conn, 2) == 2
            with pytest.raises(DatabaseError) as caught:
                await query[int](t"SELECT count(*)::int4 FROM scratch").one(conn)
            assert caught.value.sqlstate == UNDEFINED_TABLE
        after = pool.size
        await pool.close()
        return after, pool.idle, pool.waiting, pool.size, pool.closed, directory

    after, _, waiting, _, closed, directory = run(main)
    assert after == 1
    assert waiting == 0
    assert closed
    assert not directory.exists()


def test_a_pool_serves_queries_streams_and_transactions() -> None:
    async def main() -> tuple[list[int], list[int], int]:
        async with Pool("embedded://") as pool:
            await query[tuple[()]](t"CREATE TABLE numbers (n int)").execute(pool)
            async with pool.transaction() as tx:
                await query[tuple[()]](t"INSERT INTO numbers SELECT generate_series(1, 5)").execute(tx)
            rows = await query[int](t"SELECT n FROM numbers ORDER BY n").all(pool)
            streamed = [value async for value in query[int](t"SELECT n FROM numbers ORDER BY n").fetch(pool)]
            sequential = await asyncio.gather(*(number(pool, value) for value in range(8)))
            return rows, streamed, sum(sequential)

    rows, streamed, total = run(main)
    assert rows == streamed == [1, 2, 3, 4, 5]
    assert total == sum(range(8))


def test_a_pool_started_in_one_loop_serves_the_next() -> None:
    pool = Pool("embedded://")

    async def first() -> int:
        return await number(pool, 1)

    async def second() -> int:
        return await number(pool, 2)

    assert (run(first), run(second)) == (1, 2)
    run(pool.close)
    assert pool.closed


def test_a_shared_instance_serves_sequential_connections() -> None:
    async def main() -> list[int]:
        async with await embedded.start() as db:
            async with await connect(db.dsn) as conn:
                await query[tuple[()]](t"CREATE TABLE shared (n int)").execute(conn)
                await query[tuple[()]](t"INSERT INTO shared VALUES (1)").execute(conn)
            async with await connect(db.dsn) as conn:
                await query[tuple[()]](t"INSERT INTO shared VALUES (2)").execute(conn)
            async with await connect(db.dsn) as conn:
                return await query[int](t"SELECT n FROM shared ORDER BY n").all(conn)

    assert run(main) == [1, 2]


def test_start_with_a_directory_persists(tmp_path: Path) -> None:
    directory = tmp_path / "db"

    async def main() -> int:
        async with await embedded.start(directory=directory) as db, await connect(db.dsn) as conn:
            await query[tuple[()]](t"CREATE TABLE kept (n int)").execute(conn)
            await query[tuple[()]](t"INSERT INTO kept VALUES (5)").execute(conn)
        async with await embedded.start(directory=str(directory)) as db, await connect(db.dsn) as conn:
            return await query[int](t"SELECT n FROM kept").one(conn)

    assert run(main) == 5


def test_a_second_concurrent_session_is_53300() -> None:
    async def main() -> DatabaseError:
        async with await embedded.start() as db, await connect(db.dsn):
            with pytest.raises(DatabaseError) as caught:
                await connect(db.dsn)
            return caught.value

    error = run(main)
    assert error.sqlstate == TOO_MANY_CONNECTIONS
    assert "max_size=1" in (error.hint or "")


def test_a_pool_against_a_busy_instance_times_out_with_the_53300_cause() -> None:
    async def main() -> PoolTimeoutError:
        async with await embedded.start() as db, await connect(db.dsn):
            pool = Pool(db.dsn, max_size=1, acquire_timeout=3)
            try:
                with pytest.raises(PoolTimeoutError) as caught:
                    await number(pool, 1)
            finally:
                await pool.close()
            return caught.value

    error = run(main)
    cause = error.__cause__
    assert isinstance(cause, DatabaseError)
    assert cause.sqlstate == TOO_MANY_CONNECTIONS
    assert "max_size=1" in (cause.hint or "")
    assert TOO_MANY_CONNECTIONS in str(error)


def test_an_embedded_pool_that_cannot_connect_names_the_database_error() -> None:
    async def main() -> PoolTimeoutError:
        pool = Pool("embedded://?dbname=nope", acquire_timeout=3)
        try:
            # the boot is not under the acquire timeout here, so a slow machine cannot turn the cause into a timeout
            await pool.open()
            with pytest.raises(PoolTimeoutError) as caught:
                await number(pool, 1)
        finally:
            await pool.close()
        return caught.value

    cause = run(main).__cause__
    assert isinstance(cause, DatabaseError)
    assert cause.sqlstate == "3D000"


def test_pool_close_returns_with_the_directory_free(tmp_path: Path) -> None:
    url = directory_url(tmp_path / "db")

    async def main() -> None:
        pool = Pool(url)
        opening = asyncio.ensure_future(pool.open())
        await asyncio.sleep(0.05)
        await pool.close()
        async with await connect(url) as conn:
            assert await number(conn, 1) == 1
        with pytest.raises(InterfaceError, match="closed"):
            await opening

    run(main)


def test_an_instance_used_in_a_forked_child_raises_and_the_parents_survives() -> None:
    async def main() -> int:
        db = await embedded.start()
        directory = socket_directory(db.dsn)

        def child() -> int:
            try:
                _ = db.dsn
            except InterfaceError:
                pass
            else:
                return 1
            # the loop's executor threads don't exist in the child, so it closes without awaiting
            db.close_blocking()
            return 0 if directory.exists() and not db.closed else 2

        with warnings.catch_warnings():
            # the process has engine threads; the child only checks and exits
            warnings.simplefilter("ignore", DeprecationWarning)
            pid = os.fork()
        if pid == 0:
            code = 3
            try:
                code = child()
            finally:
                os._exit(code)
        _, status = await asyncio.to_thread(os.waitpid, pid, 0)
        try:
            assert os.waitstatus_to_exitcode(status) == 0
            async with await connect(db.dsn) as conn:
                return await number(conn, 3)
        finally:
            await db.close()

    assert run(main) == 3


def test_an_interrupted_memory_script_exits_without_waiting_for_its_statement() -> None:
    with subprocess.Popen(  # ruff: ignore[subprocess-without-shell-equals-true]
        [sys.executable, "-c", SLEEPER], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    ) as child:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
        time.sleep(0.5)
        started = time.monotonic()
        child.send_signal(signal.SIGINT)
        child.communicate(timeout=CHILD_WAIT)
        elapsed = time.monotonic() - started
    assert child.returncode == -signal.SIGINT
    assert elapsed < INTERRUPT_LIMIT

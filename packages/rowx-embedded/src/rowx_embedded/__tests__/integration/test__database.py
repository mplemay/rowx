import asyncio
import os
import signal
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
import time
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Final
from urllib.parse import parse_qs, urlparse

import pytest
from rowx_core import query
from rowx_core.errors import DatabaseError, InterfaceError, PoolTimeoutError
from rowx_embedded import Database
from rowx_postgres import Connection, ConnectOptions, Migrator, Pool, connect

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

TOO_MANY_CONNECTIONS: Final[str] = "53300"
UNDEFINED_TABLE: Final[str] = "42P01"
POLL_LIMIT: Final[int] = 1000
POLL_INTERVAL: Final[float] = 0.01
INTERRUPT_LIMIT: Final[float] = 8.0
CHILD_WAIT: Final[float] = 60.0
SLEEPER: Final[str] = (
    "import asyncio\n"
    "from rowx_core import query\n"
    "from rowx_embedded import Database\n"
    "async def main():\n"
    "    async with Database() as db, db.acquire() as c:\n"
    "        print('ready', flush=True)\n"
    "        await query(t'SELECT pg_sleep(30)').execute(c)\n"
    "asyncio.run(main())\n"
)


def socket_directory(dsn: str) -> Path:
    return Path(parse_qs(urlparse(dsn).query)["host"][0])


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


def test_a_memory_database_round_trips_and_close_closes_the_instance() -> None:
    async def main() -> tuple[Path, bool]:
        db = Database()
        await db.open()
        directory = socket_directory(db.dsn)
        await query[tuple[()]](t"CREATE TABLE users (id int PRIMARY KEY, name text NOT NULL)").execute(db)
        await query[tuple[()]](t"INSERT INTO users VALUES (1, 'ada')").execute(db)
        assert await query[tuple[int, str]](t"SELECT id, name FROM users").one(db) == (1, "ada")
        existed = directory.exists()
        await db.close()
        assert db.closed
        return directory, existed

    directory, existed = run(main)
    assert existed
    assert not directory.exists()


def test_the_context_manager_closes_the_instance() -> None:
    async def main() -> Path:
        async with Database() as db:
            directory = socket_directory(db.dsn)
            assert directory.exists()
            assert db.ready
        return directory

    assert not run(main).exists()


def test_two_memory_databases_are_two_independent_databases() -> None:
    async def main() -> str:
        async with Database() as first, Database() as second:
            await query[tuple[()]](t"CREATE TABLE only_in_first (id int)").execute(first)
            with pytest.raises(DatabaseError) as caught:
                await count_only_in_first(second)
            return caught.value.sqlstate

    assert run(main) == UNDEFINED_TABLE


async def count_only_in_first(conn: Connection | Pool) -> int:
    return await query[int](t"SELECT count(*)::int4 FROM only_in_first").one(conn)


def test_a_directory_persists_between_databases(tmp_path: Path) -> None:
    path = tmp_path / "db"

    async def main() -> list[tuple[int, str]]:
        async with Database(path) as db:
            await query[tuple[()]](t"CREATE TABLE kept (id int PRIMARY KEY, name text)").execute(db)
            await query[tuple[()]](t"INSERT INTO kept VALUES (1, 'ada'), (2, 'grace')").execute(db)
        async with Database(str(path)) as db:
            return await query[tuple[int, str]](t"SELECT id, name FROM kept ORDER BY id").all(db)

    assert run(main) == [(1, "ada"), (2, "grace")]


def test_a_directory_has_one_owner(tmp_path: Path) -> None:
    path = tmp_path / "db"

    async def main() -> str:
        async with Database(path):
            with pytest.raises(InterfaceError) as caught:
                await Database(path).open()
            return str(caught.value)

    assert "is in use by" in run(main)


def test_a_failed_open_leaves_no_instance_behind(tmp_path: Path) -> None:
    path = tmp_path / "db"

    async def main() -> None:
        db = Database(path, options=ConnectOptions(database="nope"), acquire_timeout=10)
        await db.open()
        with pytest.raises(PoolTimeoutError):
            await number(db, 1)
        await db.close()
        # the directory is free again
        async with Database(path) as again:
            assert await number(again, 1) == 1

    run(main)


def test_options_reach_the_session() -> None:
    async def main() -> tuple[str, str]:
        options = ConnectOptions(application_name="svc", server_settings={"search_path": "pg_catalog"})
        async with Database(options=options) as db:
            name = await query[str](t"SELECT current_setting('application_name')").one(db)
            path = await query[str](t"SELECT current_setting('search_path')").one(db)
            return name, path

    assert run(main) == ("svc", "pg_catalog")


def test_a_database_reuses_its_one_connection_and_closes_its_instance() -> None:
    async def main() -> tuple[int, int, bool, Path]:
        db = Database()
        assert (db.size, db.max_size, db.min_size) == (0, 1, 0)
        async with db.acquire() as conn:
            await query[tuple[()]](t"CREATE TEMP TABLE scratch (id int)").execute(conn)
            await query[tuple[()]](t"INSERT INTO scratch VALUES (1)").execute(conn)
        await until_idle(db)
        assert (db.size, db.idle) == (1, 1)
        directory = socket_directory(db.dsn)
        assert directory.exists()
        # the release reset the session, so the temp table is gone, but it is the same instance
        async with db.acquire() as conn:
            assert await number(conn, 2) == 2
            with pytest.raises(DatabaseError) as caught:
                await query[int](t"SELECT count(*)::int4 FROM scratch").one(conn)
            assert caught.value.sqlstate == UNDEFINED_TABLE
        after = db.size
        await db.close()
        return after, db.waiting, db.closed, directory

    after, waiting, closed, directory = run(main)
    assert after == 1
    assert waiting == 0
    assert closed
    assert not directory.exists()


def test_a_database_serves_queries_streams_and_transactions() -> None:
    async def main() -> tuple[list[int], list[int], int]:
        async with Database() as db:
            await query[tuple[()]](t"CREATE TABLE numbers (n int)").execute(db)
            async with db.transaction() as tx:
                await query[tuple[()]](t"INSERT INTO numbers SELECT generate_series(1, 5)").execute(tx)
            rows = await query[int](t"SELECT n FROM numbers ORDER BY n").all(db)
            streamed = [value async for value in query[int](t"SELECT n FROM numbers ORDER BY n").fetch(db)]
            sequential = await asyncio.gather(*(number(db, value) for value in range(8)))
            return rows, streamed, sum(sequential)

    rows, streamed, total = run(main)
    assert rows == streamed == [1, 2, 3, 4, 5]
    assert total == sum(range(8))


def test_a_database_started_in_one_loop_serves_the_next() -> None:
    db = Database()

    async def first() -> int:
        return await number(db, 1)

    async def second() -> int:
        return await number(db, 2)

    assert (run(first), run(second)) == (1, 2)
    run(db.close)
    assert db.closed


def test_the_dsn_serves_sequential_connections_while_the_database_holds_no_session() -> None:
    async def main() -> list[int]:
        async with Database() as db:
            async with await connect(db.dsn) as conn:
                await query[tuple[()]](t"CREATE TABLE shared (n int)").execute(conn)
                await query[tuple[()]](t"INSERT INTO shared VALUES (1)").execute(conn)
            async with await connect(db.dsn) as conn:
                await query[tuple[()]](t"INSERT INTO shared VALUES (2)").execute(conn)
            async with await connect(db.dsn) as conn:
                return await query[int](t"SELECT n FROM shared ORDER BY n").all(conn)

    assert run(main) == [1, 2]


def test_an_expired_idle_connection_frees_the_session_for_the_dsn() -> None:
    async def main() -> int:
        async with Database() as db:
            await number(db, 1)
            await until_idle(db)
            db.expire_connections()
            for _ in range(POLL_LIMIT):
                if db.size == 0:
                    break
                await asyncio.sleep(POLL_INTERVAL)
            async with await connect(db.dsn) as conn:
                return await number(conn, 4)

    assert run(main) == 4


def test_a_second_concurrent_session_is_53300() -> None:
    async def main() -> DatabaseError:
        async with Database() as db, db.acquire():
            with pytest.raises(DatabaseError) as caught:
                await connect(db.dsn)
            return caught.value

    error = run(main)
    assert error.sqlstate == TOO_MANY_CONNECTIONS
    assert "max_size=1" in (error.hint or "")


def test_a_pool_against_a_busy_instance_times_out_with_the_53300_cause() -> None:
    async def main() -> PoolTimeoutError:
        async with Database() as db, db.acquire():
            pool = Pool(db.dsn, max_size=1, acquire_timeout=10)
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


def test_a_database_that_cannot_connect_names_the_database_error() -> None:
    async def main() -> PoolTimeoutError:
        db = Database(options=ConnectOptions(database="nope"), acquire_timeout=10)
        try:
            # the boot is not under the acquire timeout here, so a slow machine cannot turn the cause into a timeout
            await db.open()
            with pytest.raises(PoolTimeoutError) as caught:
                await number(db, 1)
        finally:
            await db.close()
        return caught.value

    cause = run(main).__cause__
    assert isinstance(cause, DatabaseError)
    assert cause.sqlstate == "3D000"


def test_close_returns_with_the_directory_free(tmp_path: Path) -> None:
    path = tmp_path / "db"

    async def main() -> None:
        db = Database(path)
        opening = asyncio.ensure_future(db.open())
        await asyncio.sleep(0.05)
        await db.close()
        async with Database(path) as again:
            assert await number(again, 1) == 1
        with pytest.raises(InterfaceError, match="closed"):
            await opening

    run(main)


def test_a_migrator_runs_on_a_database_that_has_not_been_opened(tmp_path: Path) -> None:
    (tmp_path / "1_migrated.sql").write_text("CREATE TABLE migrated (n int4);", encoding="utf-8")

    async def main() -> tuple[int, int]:
        db = Database()
        try:
            results = await Migrator(tmp_path).run(db)
            count = await query[int](t"SELECT count(*)::int4 FROM migrated").one(db)
        finally:
            await db.close()
        return len(results), count

    assert run(main) == (1, 0)


def test_a_database_used_in_a_forked_child_raises_and_the_parents_survives() -> None:
    async def main() -> int:
        db = Database()
        await db.open()
        directory = socket_directory(db.dsn)

        def child() -> int:
            try:
                _ = db.dsn
            except InterfaceError:
                pass
            else:
                return 1
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
    # the exit guard leaves an abandoned engine's process with `os._exit`, so it reports the shell's status for an interrupt
    assert child.returncode in {-signal.SIGINT, 128 + signal.SIGINT}
    assert elapsed < INTERRUPT_LIMIT

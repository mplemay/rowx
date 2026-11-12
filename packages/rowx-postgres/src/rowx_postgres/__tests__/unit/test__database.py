import asyncio
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
import threading

import pytest
from rowx_core.errors import InterfaceError, PoolTimeoutError
from rowx_postgres import Connection, ConnectOptions, Pool, _embedded as embedded_module
from rowx_postgres.__tests__.support.fake_engine import (
    SOCKET_DSN,
    WAIT_SECONDS,
    FakeDirectoryInUseError,
    FakeEngine,
    fork_and_run,
    run,
)
from rowx_postgres._database import with_socket
from rowx_postgres.embedded import Database


def test_a_database_starts_its_instance_on_first_use_once_and_closes_it(engine: FakeEngine) -> None:
    async def main() -> None:
        pool = Database("/var/lib/db")
        assert engine.started == []
        await asyncio.gather(pool.open(), pool.open(), pool.open())
        assert [item.directory for item in engine.started] == ["/var/lib/db"]
        assert not pool.closed
        await pool.close()
        assert pool.closed
        await pool.close()

    run(main)
    assert engine.started[0].closes == 1


def test_a_database_starts_across_event_loops(engine: FakeEngine) -> None:
    pool = Database()
    run(pool.open)
    run(pool.open)
    assert len(engine.started) == 1
    run(pool.close)
    assert engine.started[0].closes == 1


def test_a_closed_pool_never_starts_an_instance(engine: FakeEngine) -> None:
    pool = Database()
    run(pool.close)
    assert pool.closed
    with pytest.raises(InterfaceError, match="closed"):
        run(pool.open)
    assert engine.started == []


def test_terminate_closes_the_instance(engine: FakeEngine) -> None:
    pool = Database()
    run(pool.open)
    pool.terminate()
    assert pool.closed
    assert engine.started[0].closes == 1


def test_terminate_before_the_first_use_starts_nothing(engine: FakeEngine) -> None:
    pool = Database()
    pool.terminate()
    with pytest.raises(InterfaceError, match="closed"):
        run(pool.open)
    assert engine.started == []


def test_a_database_close_waits_for_the_start_in_flight_and_returns_with_the_instance_gone(engine: FakeEngine) -> None:
    engine.gate = threading.Event()

    async def main() -> None:
        pool = Database()
        opening = asyncio.ensure_future(pool.open())
        await asyncio.to_thread(engine.entered.wait, WAIT_SECONDS)
        closing = asyncio.ensure_future(pool.close())
        # the start cannot finish while the gate is shut, so close() must still be waiting for it
        await asyncio.sleep(0.05)
        assert not closing.done()
        assert engine.gate is not None
        engine.gate.set()
        await closing
        assert engine.started[0].closes == 1
        with pytest.raises(InterfaceError, match="closed"):
            await opening

    run(main)


def test_terminate_racing_the_start_leaves_no_instance(engine: FakeEngine) -> None:
    engine.gate = threading.Event()

    async def main() -> None:
        pool = Database()
        opening = asyncio.ensure_future(pool.open())
        await asyncio.to_thread(engine.entered.wait, WAIT_SECONDS)
        pool.terminate()
        assert pool.closed
        assert engine.gate is not None
        engine.gate.set()
        with pytest.raises(InterfaceError, match="closed"):
            await opening
        # the start thread closed the instance it had just booted
        assert await asyncio.to_thread(engine.started[0].closed_event.wait, WAIT_SECONDS)

    run(main)
    assert engine.started[0].closes == 1


def test_a_cancelled_first_use_leaves_a_pool_that_a_later_use_completes(engine: FakeEngine) -> None:
    engine.gate = threading.Event()

    async def main() -> None:
        pool = Database()
        first = asyncio.ensure_future(pool.open())
        await asyncio.to_thread(engine.entered.wait, WAIT_SECONDS)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert engine.gate is not None
        engine.gate.set()
        await pool.open()
        assert len(engine.started) == 1
        await pool.close()

    run(main)
    assert engine.started[0].closes == 1


def test_many_first_use_callers_share_one_start_thread(engine: FakeEngine) -> None:
    engine.gate = threading.Event()
    before = threading.active_count()

    async def main() -> int:
        pool = Database()
        openers = [asyncio.ensure_future(pool.open()) for _ in range(50)]
        await asyncio.to_thread(engine.entered.wait, WAIT_SECONDS)
        await asyncio.sleep(0.05)
        # one start thread, however many callers wait (the old design parked one executor thread each)
        extra = threading.active_count() - before
        assert engine.gate is not None
        engine.gate.set()
        await asyncio.gather(*openers)
        await pool.close()
        return extra

    assert run(main) <= 2
    assert len(engine.started) == 1


def test_a_first_use_is_bounded_by_the_acquire_timeout(engine: FakeEngine) -> None:
    engine.gate = threading.Event()

    async def main() -> None:
        pool = Database(acquire_timeout=0.1)
        with pytest.raises(PoolTimeoutError, match="start"):
            async with pool.acquire():
                pass
        # the start itself carries on and the pool still owns its result
        assert engine.gate is not None
        engine.gate.set()
        await pool.open()
        await pool.close()

    run(main)
    assert engine.started[0].closes == 1


def test_hooks_work_with_the_lazy_database(engine: FakeEngine) -> None:
    calls: list[str] = []

    async def setup(conn: Connection) -> None:  # ruff: ignore[unused-async]
        del conn
        calls.append("setup")

    pool = Database("/var/lib/db", setup=setup)
    assert (pool.max_size, pool.size) == (1, 0)
    assert pool._via_python
    run(pool.open)
    assert [item.directory for item in engine.started] == ["/var/lib/db"]
    run(pool.close)
    assert engine.started[0].closes == 1


def test_a_failed_pool_start_leaves_no_instance_and_can_retry(engine: FakeEngine) -> None:
    pool = Database()
    engine.failure = FakeDirectoryInUseError("in use by another instance")
    with pytest.raises(InterfaceError, match="in use by another instance"):
        run(pool.open)
    assert not pool.closed
    engine.failure = None
    run(pool.open)
    assert len(engine.started) == 1
    run(pool.close)


def test_a_database_is_a_pool_with_one_session() -> None:
    db = Database()
    assert isinstance(db, Pool)
    assert (db.min_size, db.max_size, db.directory, db.ready, db.closed) == (0, 1, None, False, False)


@pytest.mark.parametrize("max_size", [0, 2, 10])
def test_a_database_of_any_other_size_than_one_is_a_value_error(max_size: int) -> None:
    with pytest.raises(ValueError, match="one session at a time"):
        Database(max_size=max_size)


@pytest.mark.parametrize(
    ("name", "options"),
    [
        ("host", ConnectOptions(host="x")),
        ("port", ConnectOptions(port=5432)),
        ("user", ConnectOptions(user="x")),
        ("password", ConnectOptions(password="x")),  # ruff: ignore[hardcoded-password-func-arg]
        ("sslmode", ConnectOptions(sslmode="require")),
    ],
)
def test_options_that_name_a_server_are_a_value_error(name: str, options: ConnectOptions) -> None:
    with pytest.raises(ValueError, match=f"{name} cannot be set for an embedded database"):
        Database(options=options)


def test_the_directory_is_normalised_and_validated_in_the_constructor() -> None:
    assert Database("/var//lib/db/").directory == "/var/lib/db"
    with pytest.raises(ValueError, match="absolute"):
        Database("relative/db")
    with pytest.raises(ValueError, match="root"):
        Database("/")


def test_a_database_still_validates_the_usual_bounds() -> None:
    with pytest.raises(ValueError, match="acquire_timeout must not be negative"):
        Database(acquire_timeout=-1)


def test_a_constructed_database_does_no_io_and_never_loads_the_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail() -> None:
        pytest.fail("the engine was loaded")

    monkeypatch.setattr(embedded_module, "load_cached", fail)
    db = Database("/var/lib/db")
    assert (db.size, db.idle, db.waiting, db.closed, db.ready) == (0, 0, 0, False, False)
    db.expire_connections()


def test_a_module_level_database_imports_without_the_companion() -> None:
    code = (
        "import sys\n"
        "sys.modules['rowx_embedded'] = None\n"
        "from rowx_postgres.embedded import Database\n"
        "db = Database()\n"
        "assert db.max_size == 1 and db.size == 0 and not db.closed and not db.ready\n"
    )
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_ready_and_wait_ready_follow_the_start(engine: FakeEngine) -> None:
    async def main() -> None:
        db = Database()
        assert not db.ready
        await db.wait_ready()
        assert db.ready
        await db.wait_ready()
        await db.close()
        assert not db.ready
        assert db.closed

    run(main)
    assert len(engine.started) == 1


def test_the_dsn_needs_a_started_open_database(engine: FakeEngine) -> None:
    async def main() -> None:
        db = Database()
        with pytest.raises(InterfaceError, match=r"call open\(\) first"):
            _ = db.dsn
        await db.open()
        assert db.dsn == SOCKET_DSN
        await db.close()
        with pytest.raises(InterfaceError, match="closed"):
            _ = db.dsn

    run(main)
    assert engine.started[0].closes == 1


def test_a_never_opened_database_closes_cleanly(engine: FakeEngine) -> None:
    db = Database()
    run(db.close)
    assert db.closed
    assert engine.started == []


def test_the_dsn_of_a_forked_child_raises(engine: FakeEngine) -> None:
    db = Database()
    run(db.open)

    def child() -> int:
        try:
            _ = db.dsn
        except InterfaceError:
            return 0
        return 1

    assert fork_and_run(child) == 0
    assert db.dsn == SOCKET_DSN
    run(db.close)
    assert engine.started[0].closes == 1


def test_options_are_pointed_at_the_instance_socket() -> None:
    options = ConnectOptions(application_name="svc", server_settings={"search_path": "app"})
    socket = with_socket(options, SOCKET_DSN)
    assert socket.host == "/nonexistent/rowx-socket"
    assert (socket.user, socket.database, socket.sslmode) == ("postgres", "postgres", "disable")
    assert (socket.application_name, dict(socket.server_settings)) == ("svc", {"search_path": "app"})
    assert with_socket(ConnectOptions(database="other"), SOCKET_DSN).database == "other"


def test_the_native_pool_is_built_from_the_options_on_the_socket(
    engine: FakeEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del engine
    seen: list[ConnectOptions] = []

    class FakeImpl:
        def __init__(self, options: ConnectOptions, **_: object) -> None:
            seen.append(options)
            self.opened = False
            self.closed = False

        async def open(self) -> None:
            self.opened = True

        async def close(self, limit: float | None, /) -> None:
            del limit
            self.closed = True

    monkeypatch.setattr("rowx_postgres._pool.PoolImpl", FakeImpl)
    options = ConnectOptions(server_settings={"search_path": "app"})
    db = Database(options=options)
    run(db.open)
    run(db.close)
    assert [(item.host, dict(item.server_settings)) for item in seen] == [
        ("/nonexistent/rowx-socket", {"search_path": "app"}),
    ]

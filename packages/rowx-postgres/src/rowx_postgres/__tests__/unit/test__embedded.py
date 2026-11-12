import asyncio
import os
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
import threading
import time
import warnings
from dataclasses import replace
from types import ModuleType
from typing import TYPE_CHECKING, Any, Final, override

import pytest
from rowx_core.errors import InterfaceError, PoolTimeoutError
from rowx_postgres import (
    Connection,
    ConnectOptions,
    Pool,
    _connection as connection_module,
    _embedded as embedded_module,
    connect,
    embedded,
)
from rowx_postgres._admin import with_database
from rowx_postgres._embedded import EmbeddedTarget, parse_embedded_url, reject_embedded
from rowx_postgres._services import PostgresAdmin, PostgresMigrations, PostgresProvisioner, open_checker

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

SOCKET_DSN: Final[str] = "postgresql://postgres@/postgres?host=/nonexistent/rowx-socket&sslmode=disable"
WAIT_SECONDS: Final[float] = 10.0


class FakeEngineError(Exception):
    pass


class FakeDirectoryInUseError(FakeEngineError):
    pass


class FakeSnapshotError(FakeEngineError):
    pass


class FakeInstance:
    def __init__(self, directory: str | None, snapshot: bytes | None) -> None:
        self.directory = directory
        self.booted_from = snapshot
        self.dsn = SOCKET_DSN
        self.closes = 0
        self.abandoned = False
        self.closed_event = threading.Event()
        self.entered_close = threading.Event()
        self.close_gate: threading.Event | None = None
        self.snapshot_bytes = b"snapshot"
        self.abandon_forced = False

    def snapshot(self) -> bytes:
        return self.snapshot_bytes

    def abandon(self, *, force: bool = False) -> None:
        self.abandon_forced = force
        self.abandoned = True
        self.closed_event.set()

    def close(self) -> None:
        self.closes += 1
        if self.close_gate is not None:
            assert self.close_gate.wait(WAIT_SECONDS)
        self.closed_event.set()


class FakeEngine(ModuleType):
    def __init__(self) -> None:
        super().__init__("rowx_embedded")
        self.API_VERSION = embedded_module.EXPECTED_API_VERSION
        self.EngineError = FakeEngineError
        self.DirectoryInUseError = FakeDirectoryInUseError
        self.SnapshotError = FakeSnapshotError
        self.Instance = self
        self.started: list[FakeInstance] = []
        self.failure: Exception | None = None
        self.gate: threading.Event | None = None
        self.entered = threading.Event()

    def start(self, directory: str | None = None, snapshot: bytes | None = None) -> FakeInstance:
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(WAIT_SECONDS)
        if self.failure is not None:
            raise self.failure
        instance = FakeInstance(directory, snapshot)
        self.started.append(instance)
        return instance


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeEngine]:
    fake = FakeEngine()
    monkeypatch.setitem(sys.modules, "rowx_embedded", fake)
    embedded_module.load_cached.cache_clear()
    yield fake
    embedded_module.load_cached.cache_clear()


class FakeConnection:
    def __init__(self) -> None:
        self.closes = 0

    async def close(self) -> None:
        self.closes += 1

    @property
    def closed(self) -> bool:
        return self.closes > 0


def run[T](factory: Callable[[], Any]) -> T:
    return asyncio.run(factory())


# --- the URL grammar -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "directory"),
    [
        ("embedded://", None),
        ("embedded:", None),
        ("EMBEDDED://", None),
        ("embedded:///var/lib/app/db", "/var/lib/app/db"),
        ("embedded:///var/lib/my%20db", "/var/lib/my db"),
        ("embedded:///var/lib/app/db?application_name=x", "/var/lib/app/db"),
    ],
)
def test_a_memory_or_absolute_directory_url_parses(url: str, directory: str | None) -> None:
    options = ConnectOptions.from_dsn(url)
    assert options.embedded == EmbeddedTarget(directory=directory)
    assert (options.host, options.port, options.user, options.password, options.sslmode) == (None,) * 5


@pytest.mark.parametrize(
    "url",
    [
        "embedded://localhost",
        "embedded://localhost/var/lib/db",
        "embedded://:5432",
        "embedded://user@",
        "embedded://user:secret@/var/lib/db",
        "embedded://[::1]/db",
    ],
)
def test_a_non_empty_netloc_is_a_value_error(url: str) -> None:
    with pytest.raises(ValueError, match="no host, port, user or password"):
        ConnectOptions.from_dsn(url)


@pytest.mark.parametrize("url", ["embedded:relative/db", "embedded:db", "embedded:./db"])
def test_a_relative_path_is_a_value_error(url: str) -> None:
    with pytest.raises(ValueError, match="absolute"):
        ConnectOptions.from_dsn(url)


@pytest.mark.parametrize("name", ["sslmode", "SSLMODE", "host", "hostaddr", "port", "user", "password"])
def test_a_reserved_query_parameter_is_a_value_error_that_never_echoes_the_value(name: str) -> None:
    with pytest.raises(ValueError, match=name) as caught:
        ConnectOptions.from_dsn(f"embedded://?{name}=Zq9value")
    assert "Zq9value" not in str(caught.value)


def test_a_parse_of_a_url_that_is_not_embedded_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="expected"):
        parse_embedded_url("postgresql://h/db")


# --- overrides, replace() and forwarded parameters ---------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [{"host": "h"}, {"port": 5432}, {"user": "u"}, {"password": "p"}, {"sslmode": "require"}],
)
def test_overrides_of_the_socket_are_a_value_error(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=next(iter(overrides))):
        ConnectOptions.from_dsn("embedded://", **overrides)


@pytest.mark.parametrize(
    "overrides",
    [{"host": "h"}, {"port": 5432}, {"user": "u"}, {"password": "p"}, {"sslmode": "require"}],
)
def test_replace_of_the_socket_is_a_value_error(overrides: dict[str, object]) -> None:
    options = ConnectOptions.from_dsn("embedded://")
    with pytest.raises(ValueError, match=next(iter(overrides))):
        replace(options, **overrides)


def test_an_embedded_options_object_cannot_be_built_with_a_socket_either() -> None:
    with pytest.raises(ValueError, match="host"):
        ConnectOptions(host="h", embedded=EmbeddedTarget())


def test_other_overrides_are_allowed() -> None:
    options = ConnectOptions.from_dsn("embedded://", connect_timeout=3.0, statement_cache_size=8)
    assert replace(options, application_name="app").embedded == EmbeddedTarget()
    assert (options.connect_timeout, options.statement_cache_size) == (3.0, 8)


def test_other_parameters_are_forwarded_to_the_socket_options() -> None:
    options = ConnectOptions.from_dsn(
        "embedded:///var/lib/db?application_name=svc&connect_timeout=4&options=-c%20search_path%3Dapp",
    )
    assert options.application_name == "svc"
    assert options.connect_timeout == pytest.approx(4.0)
    assert options.server_settings == {"search_path": "app"}
    socket = options.with_socket(SOCKET_DSN)
    assert socket.embedded is None
    assert socket.host == "/nonexistent/rowx-socket"
    assert (socket.user, socket.database, socket.sslmode) == ("postgres", "postgres", "disable")
    assert socket.application_name == "svc"
    assert socket.server_settings == {"search_path": "app"}


def test_a_dbname_parameter_wins_over_the_socket_database() -> None:
    assert ConnectOptions.from_dsn("embedded://?dbname=other").with_socket(SOCKET_DSN).database == "other"


def test_the_repr_names_the_directory_and_hides_nothing_new() -> None:
    assert "embedded=EmbeddedTarget(directory='/var/lib/db')" in repr(ConnectOptions.from_dsn("embedded:///var/lib/db"))


# --- the engine load ---------------------------------------------------------------------------------------------


def test_a_missing_companion_is_an_interface_error_with_the_install_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "rowx_embedded", None)
    monkeypatch.setattr(embedded_module, "current_platform", lambda: ("linux", "x86_64"))
    embedded_module.load_cached.cache_clear()
    with pytest.raises(InterfaceError) as caught:
        run(lambda: connect("embedded://"))
    assert 'help: uv add "rowx[embedded]"' in str(caught.value)
    with pytest.raises(InterfaceError, match=r'uv add "rowx\[embedded\]"'):
        run(embedded.start)


def test_an_unsupported_platform_is_an_interface_error_without_the_install_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "rowx_embedded", None)
    monkeypatch.setattr(embedded_module, "current_platform", lambda: ("win32", "amd64"))
    embedded_module.load_cached.cache_clear()
    with pytest.raises(InterfaceError, match="not available on this platform") as caught:
        run(lambda: connect("embedded://"))
    assert "uv add" not in str(caught.value)


def test_an_api_version_mismatch_names_both_versions(engine: FakeEngine) -> None:
    engine.API_VERSION = 99
    with pytest.raises(InterfaceError) as caught:
        run(embedded.start)
    message = str(caught.value)
    assert "99" in message
    assert str(embedded_module.EXPECTED_API_VERSION) in message
    assert engine.started == []


def test_a_directory_in_use_is_an_interface_error_with_the_engine_message(engine: FakeEngine) -> None:
    engine.failure = FakeDirectoryInUseError("/var/lib/db is in use by pid 42")
    with pytest.raises(InterfaceError, match="is in use by pid 42"):
        run(lambda: embedded.start(directory="/var/lib/db"))
    engine.failure = FakeDirectoryInUseError("/var/lib/db is in use by another instance")
    with pytest.raises(InterfaceError, match="in use by another instance"):
        run(lambda: connect("embedded:///var/lib/db"))


def test_a_relative_start_directory_is_a_value_error(engine: FakeEngine) -> None:
    with pytest.raises(ValueError, match="absolute"):
        run(lambda: embedded.start(directory="relative/db"))
    assert engine.started == []


def test_start_returns_a_database_that_closes_once(engine: FakeEngine) -> None:
    async def main() -> str:
        async with await embedded.start(directory="/var/lib/db") as db:
            assert not db.closed
            dsn = db.dsn
        await db.close()
        assert db.closed
        with pytest.raises(InterfaceError, match="closed"):
            _ = db.dsn
        return dsn

    assert run(main) == SOCKET_DSN
    assert [(item.directory, item.closes) for item in engine.started] == [("/var/lib/db", 1)]


def test_a_rejected_snapshot_start_is_marked_so_it_can_be_rebuilt(engine: FakeEngine) -> None:
    engine.failure = FakeSnapshotError("built by another engine")
    with pytest.raises(embedded_module.SnapshotRejectedError, match="another engine"):
        run(lambda: embedded_module.start_from_snapshot(b"seed"))


def test_a_snapshot_error_without_a_snapshot_and_other_start_errors_are_plain(engine: FakeEngine) -> None:
    engine.failure = FakeSnapshotError("odd")
    with pytest.raises(InterfaceError, match="odd") as plain:
        run(embedded_module.start)
    assert not isinstance(plain.value, embedded_module.SnapshotRejectedError)
    engine.failure = FakeEngineError("other")
    with pytest.raises(InterfaceError, match="other") as other:
        run(lambda: embedded_module.start_from_snapshot(b"seed"))
    assert not isinstance(other.value, embedded_module.SnapshotRejectedError)


def test_the_private_snapshot_helpers_start_from_bytes_and_take_a_snapshot(engine: FakeEngine) -> None:
    async def main() -> bytes:
        db = await embedded_module.start_from_snapshot(b"seed")
        try:
            return await embedded_module.take_snapshot(db)
        finally:
            await db.close()

    assert run(main) == b"snapshot"
    assert engine.started[0].booted_from == b"seed"


# --- ownership: connect ------------------------------------------------------------------------------------------


def test_a_connection_owns_its_instance_and_closes_it(engine: FakeEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[ConnectOptions] = []

    async def fake_connect(options: ConnectOptions) -> FakeConnection:  # ruff: ignore[unused-async]
        seen.append(options)
        return FakeConnection()

    monkeypatch.setattr(connection_module, "connect_impl", fake_connect)

    async def main() -> None:
        conn = await connect("embedded://?application_name=svc")
        assert engine.started[0].closes == 0
        await conn.close()
        await conn.close()

    run(main)
    assert engine.started[0].closes == 1
    assert [(item.host, item.embedded, item.application_name) for item in seen] == [
        ("/nonexistent/rowx-socket", None, "svc"),
    ]


def test_two_memory_connects_start_two_instances(engine: FakeEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_connect(options: ConnectOptions) -> FakeConnection:  # ruff: ignore[unused-async, unused-function-argument]
        return FakeConnection()

    monkeypatch.setattr(connection_module, "connect_impl", fake_connect)

    async def main() -> None:
        async with await connect("embedded://"), await connect("embedded://"):
            assert len(engine.started) == 2

    run(main)
    assert [item.closes for item in engine.started] == [1, 1]


def test_a_failed_connect_closes_the_instance(engine: FakeEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_connect(options: ConnectOptions) -> FakeConnection:  # ruff: ignore[unused-async, unused-function-argument]
        msg = "refused"
        raise InterfaceError(msg)

    monkeypatch.setattr(connection_module, "connect_impl", fake_connect)
    with pytest.raises(InterfaceError, match="refused"):
        run(lambda: connect("embedded://"))
    assert engine.started[0].closes == 1


def test_a_cancelled_connect_closes_the_instance(engine: FakeEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    blocked = asyncio.Event
    connecting = threading.Event()

    async def fake_connect(options: ConnectOptions) -> FakeConnection:  # ruff: ignore[unused-function-argument]
        connecting.set()
        await blocked().wait()
        return FakeConnection()

    monkeypatch.setattr(connection_module, "connect_impl", fake_connect)

    async def main() -> None:
        task = asyncio.ensure_future(connect("embedded://"))
        assert await asyncio.to_thread(connecting.wait, WAIT_SECONDS)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(main)
    # a memory instance whose connect was cancelled is abandoned, not waited for
    assert engine.started[0].closed_event.wait(WAIT_SECONDS)
    assert engine.started[0].abandoned
    assert engine.started[0].closes == 0


def test_a_connection_whose_close_raises_still_closes_its_instance(
    engine: FakeEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Failing(FakeConnection):
        @override
        async def close(self) -> None:
            msg = "the close failed"
            raise InterfaceError(msg)

    async def fake_connect(options: ConnectOptions) -> Failing:  # ruff: ignore[unused-async, unused-function-argument]
        return Failing()

    monkeypatch.setattr(connection_module, "connect_impl", fake_connect)

    async def main() -> None:
        conn = await connect("embedded://")
        with pytest.raises(InterfaceError, match="the close failed"):
            await conn.close()

    run(main)
    assert engine.started[0].closes == 1


def test_a_connect_cancelled_during_the_start_still_closes_the_instance(
    engine: FakeEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_connect(options: ConnectOptions) -> FakeConnection:  # ruff: ignore[unused-async, unused-function-argument]
        return FakeConnection()

    monkeypatch.setattr(connection_module, "connect_impl", fake_connect)
    engine.gate = threading.Event()

    async def main() -> None:
        task = asyncio.ensure_future(connect("embedded://"))
        # the engine is inside its start, which cannot be interrupted
        await asyncio.to_thread(engine.entered.wait, WAIT_SECONDS)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert engine.gate is not None
        engine.gate.set()

    run(main)
    # the memory instance that finished starting after the cancel is abandoned, not waited for
    deadline = threading.Event()
    while not engine.started and not deadline.wait(0.01):
        pass
    assert engine.started[0].closed_event.wait(WAIT_SECONDS)
    assert (engine.started[0].abandoned, engine.started[0].closes) == (True, 0)


def test_a_non_embedded_connect_never_loads_the_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[ConnectOptions] = []

    async def fake_connect(options: ConnectOptions) -> FakeConnection:  # ruff: ignore[unused-async]
        seen.append(options)
        return FakeConnection()

    def fail() -> None:
        pytest.fail("the engine was loaded")

    monkeypatch.setattr(connection_module, "connect_impl", fake_connect)
    monkeypatch.setattr(embedded_module, "load_cached", fail)
    run(lambda: connect("postgresql://u@h/db"))
    assert [item.host for item in seen] == ["h"]


# --- ownership: Pool ---------------------------------------------------------------------------------------------


def test_the_max_size_defaults_to_one_for_embedded_and_ten_otherwise() -> None:
    assert Pool("embedded://").max_size == 1
    assert Pool("embedded://", max_size=1).max_size == 1
    assert Pool("postgresql://u@h/db").max_size == 10
    assert Pool("postgresql://u@h/db", max_size=3).max_size == 3


@pytest.mark.parametrize(("min_size", "max_size"), [(0, 2), (2, None), (2, 4)])
def test_an_embedded_pool_larger_than_one_is_a_value_error(min_size: int, max_size: int | None) -> None:
    with pytest.raises(ValueError, match="one session at a time"):
        Pool("embedded://", min_size=min_size, max_size=max_size)


def test_an_embedded_pool_still_validates_the_usual_bounds() -> None:
    with pytest.raises(ValueError, match="max_size must be at least 1"):
        Pool("embedded://", max_size=0)


def test_an_embedded_pool_validates_its_url_in_the_constructor() -> None:
    with pytest.raises(ValueError, match="absolute"):
        Pool("embedded:relative")


def test_a_constructed_embedded_pool_does_no_io_and_never_loads_the_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail() -> None:
        pytest.fail("the engine was loaded")

    monkeypatch.setattr(embedded_module, "load_cached", fail)
    pool = Pool("embedded:///var/lib/db")
    assert (pool.size, pool.idle, pool.waiting, pool.min_size, pool.max_size, pool.closed) == (0, 0, 0, 0, 1, False)
    pool.expire_connections()


def test_a_module_level_embedded_pool_imports_without_the_companion() -> None:
    code = (
        "import sys\n"
        "sys.modules['rowx_embedded'] = None\n"
        "from rowx_postgres import Pool\n"
        "pool = Pool('embedded://')\n"
        "assert pool.max_size == 1 and pool.size == 0 and not pool.closed\n"
    )
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("statement", ["import rowx", "import rowx_postgres", "import rowx_postgres.embedded"])
def test_importing_never_imports_the_companion(statement: str) -> None:
    code = f"import sys\n{statement}\nassert 'rowx_embedded' not in sys.modules\n"
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_a_pool_starts_its_instance_on_first_use_once_and_closes_it(engine: FakeEngine) -> None:
    async def main() -> None:
        pool = Pool("embedded:///var/lib/db")
        assert engine.started == []
        await asyncio.gather(pool.open(), pool.open(), pool.open())
        assert [item.directory for item in engine.started] == ["/var/lib/db"]
        assert not pool.closed
        await pool.close()
        assert pool.closed
        await pool.close()

    run(main)
    assert engine.started[0].closes == 1


def test_a_pool_starts_across_event_loops(engine: FakeEngine) -> None:
    pool = Pool("embedded://")
    run(pool.open)
    run(pool.open)
    assert len(engine.started) == 1
    run(pool.close)
    assert engine.started[0].closes == 1


def test_a_closed_pool_never_starts_an_instance(engine: FakeEngine) -> None:
    pool = Pool("embedded://")
    run(pool.close)
    assert pool.closed
    with pytest.raises(InterfaceError, match="closed"):
        run(pool.open)
    assert engine.started == []


def test_terminate_closes_the_instance(engine: FakeEngine) -> None:
    pool = Pool("embedded://")
    run(pool.open)
    pool.terminate()
    assert pool.closed
    assert engine.started[0].closes == 1


def test_terminate_before_the_first_use_starts_nothing(engine: FakeEngine) -> None:
    pool = Pool("embedded://")
    pool.terminate()
    with pytest.raises(InterfaceError, match="closed"):
        run(pool.open)
    assert engine.started == []


def test_a_pool_close_waits_for_the_start_in_flight_and_returns_with_the_instance_gone(engine: FakeEngine) -> None:
    engine.gate = threading.Event()

    async def main() -> None:
        pool = Pool("embedded://")
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
        pool = Pool("embedded://")
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
        pool = Pool("embedded://")
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
        pool = Pool("embedded://")
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
        pool = Pool("embedded://", acquire_timeout=0.1)
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


def test_an_embedded_options_object_and_hooks_work_with_the_lazy_pool(engine: FakeEngine) -> None:
    calls: list[str] = []

    async def setup(conn: Connection) -> None:  # ruff: ignore[unused-async]
        del conn
        calls.append("setup")

    options = ConnectOptions.from_dsn("embedded:///var/lib/db")
    pool = Pool(options, setup=setup)
    assert (pool.max_size, pool.size) == (1, 0)
    assert pool._via_python
    run(pool.open)
    assert [item.directory for item in engine.started] == ["/var/lib/db"]
    run(pool.close)
    assert engine.started[0].closes == 1


def test_a_failed_pool_start_leaves_no_instance_and_can_retry(engine: FakeEngine) -> None:
    pool = Pool("embedded://")
    engine.failure = FakeDirectoryInUseError("in use by another instance")
    with pytest.raises(InterfaceError, match="in use by another instance"):
        run(pool.open)
    assert not pool.closed
    engine.failure = None
    run(pool.open)
    assert len(engine.started) == 1
    run(pool.close)


# --- fork safety -------------------------------------------------------------------------------------------------


def fork_and_run(child: Callable[[], int]) -> int:
    with warnings.catch_warnings():
        # forking a process that may have other threads is what these tests are about
        warnings.simplefilter("ignore", DeprecationWarning)
        pid = os.fork()
    if pid == 0:
        code = 3
        try:
            code = child()
        finally:
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


def test_a_forked_child_of_an_embedded_pool_raises_an_interface_error() -> None:
    pool = Pool("embedded://")

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

    assert fork_and_run(lambda: asyncio.run(child())) == 0


def test_a_database_used_in_a_forked_child_raises_and_its_close_leaves_the_parents_instance(
    engine: FakeEngine,
) -> None:
    db = run(embedded.start)

    def child() -> int:
        try:
            _ = db.dsn
        except InterfaceError:
            pass
        else:
            return 1
        asyncio.run(db.close())
        return 0 if engine.started[0].closes == 0 and not db.closed else 2

    assert fork_and_run(child) == 0
    assert engine.started[0].closes == 0
    assert db.dsn == SOCKET_DSN
    run(db.close)
    assert engine.started[0].closes == 1


def test_an_engine_loaded_before_a_fork_is_refused_in_the_child(engine: FakeEngine) -> None:
    run(embedded.start)

    def child() -> int:
        try:
            asyncio.run(embedded.start())
        except InterfaceError:
            return 0
        return 1

    assert fork_and_run(child) == 0
    assert len(engine.started) == 1


# --- URL edge cases, repr, the other Rust paths, the close lock ----------------------------------------------------


@pytest.mark.parametrize(
    ("url", "directory"),
    [
        ("embedded:////var/lib/db", "/var/lib/db"),
        ("embedded:///var/lib/db/", "/var/lib/db"),
        ("embedded:///var/lib/../lib/./db", "/var/lib/db"),
        ("embedded:///a//b", "/a/b"),
    ],
)
def test_the_directory_is_normalised(url: str, directory: str) -> None:
    assert ConnectOptions.from_dsn(url).embedded == EmbeddedTarget(directory=directory)


@pytest.mark.parametrize("url", ["embedded:///", "embedded:////", "embedded:///.", "embedded:///a/../.."])
def test_the_root_is_not_a_database_directory(url: str) -> None:
    with pytest.raises(ValueError, match="root"):
        ConnectOptions.from_dsn(url)


@pytest.mark.parametrize("url", ["embedded://#frag", "embedded:///var/lib/db#x", "embedded://?a=1#x"])
def test_a_fragment_is_a_value_error(url: str) -> None:
    with pytest.raises(ValueError, match="fragment"):
        ConnectOptions.from_dsn(url)


def test_a_duplicate_query_key_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="application_name"):
        ConnectOptions.from_dsn("embedded://?application_name=a&application_name=b")


def test_a_path_with_a_semicolon_is_kept_whole() -> None:
    assert ConnectOptions.from_dsn("embedded:///var/db;v1").embedded == EmbeddedTarget(directory="/var/db;v1")


def test_start_normalises_and_refuses_the_root(engine: FakeEngine) -> None:
    run(lambda: embedded.start(directory="/var//lib/db/"))
    assert engine.started[0].directory == "/var/lib/db"
    with pytest.raises(ValueError, match="root"):
        run(lambda: embedded.start(directory="/"))


def test_the_repr_of_ordinary_options_has_no_embedded_part() -> None:
    shown = repr(ConnectOptions(host="h", extra={"sslpassword": "x"}))
    assert "embedded" not in shown
    assert shown.endswith("extra={'sslpassword': '<redacted>'})")


@pytest.mark.parametrize("target", ["embedded://", "embedded:///var/lib/db", ConnectOptions.from_dsn("embedded://")])
def test_every_server_only_path_rejects_an_embedded_target_clearly(target: str | ConnectOptions) -> None:
    dsn = target if isinstance(target, str) else "embedded://"
    for build in (
        lambda: PostgresAdmin(dsn),
        lambda: PostgresProvisioner(dsn),
        lambda: with_database(dsn, "other"),
        lambda: reject_embedded(target),
    ):
        with pytest.raises(InterfaceError, match="not supported here"):
            build()
    with pytest.raises(InterfaceError, match="not supported here"):
        run(lambda: open_checker(dsn, 1.0))
    # a migrator is built from the URL for a scratch, but it never opens a session on one
    with pytest.raises(InterfaceError, match="not supported here"):
        run(PostgresMigrations(dsn, ".").open)


def test_a_server_url_is_not_rejected() -> None:
    reject_embedded("postgresql://u@h/db")
    reject_embedded(ConnectOptions(host="h"))


def test_an_unexpected_engine_failure_becomes_an_interface_error_with_its_cause(engine: FakeEngine) -> None:
    engine.failure = RuntimeError("wasm trap")
    with pytest.raises(InterfaceError, match="wasm trap") as caught:
        run(embedded.start)
    assert isinstance(caught.value.__cause__, RuntimeError)


def test_concurrent_closes_close_once_and_the_second_waits_for_the_first(engine: FakeEngine) -> None:
    release = threading.Event()
    original = FakeInstance.close

    def slow_close(self: FakeInstance) -> None:
        self.entered_close.set()
        assert release.wait(WAIT_SECONDS)
        original(self)

    db = run(embedded.start)
    instance = engine.started[0]
    instance.close = lambda: slow_close(instance)
    results: list[bool] = []

    def second() -> None:
        db.close_blocking()
        # only returns after the first close finished
        results.append(db.closed)

    first = threading.Thread(target=db.close_blocking)
    first.start()
    assert instance.entered_close.wait(WAIT_SECONDS)
    other = threading.Thread(target=second)
    other.start()
    other.join(0.1)
    assert other.is_alive()
    release.set()
    first.join(WAIT_SECONDS)
    other.join(WAIT_SECONDS)
    assert results == [True]
    assert instance.closes == 1


def test_a_failed_close_is_not_recorded_as_closed(engine: FakeEngine) -> None:
    db = run(embedded.start)
    instance = engine.started[0]
    calls = [0]

    def flaky() -> None:
        calls[0] += 1
        if calls[0] == 1:
            msg = "close failed"
            raise RuntimeError(msg)

    instance.close = flaky
    with pytest.raises(RuntimeError, match="close failed"):
        run(db.close)
    assert not db.closed
    run(db.close)
    assert db.closed
    assert calls[0] == 2


def test_abandon_stops_a_memory_instance_without_closing_it(engine: FakeEngine) -> None:
    async def main() -> None:
        db = await embedded.start()
        db.abandon()
        assert db.closed
        # idempotent with close, which then has nothing to do
        db.abandon()
        await db.close()

    run(main)
    assert (engine.started[0].abandoned, engine.started[0].closes) == (True, 0)


def test_abandon_refuses_a_directory_unless_forced(engine: FakeEngine) -> None:
    async def main() -> None:
        db = await embedded.start(directory="/var/lib/db")
        with pytest.raises(InterfaceError, match="cannot be abandoned"):
            db.abandon()
        assert not db.closed
        db.abandon(force=True)
        assert db.closed

    run(main)
    assert engine.started[0].abandoned


def test_abandon_after_a_close_does_nothing(engine: FakeEngine) -> None:
    async def main() -> None:
        db = await embedded.start()
        await db.close()
        db.abandon()

    run(main)
    assert (engine.started[0].abandoned, engine.started[0].closes) == (False, 1)


def test_abandon_in_a_forked_child_is_a_no_op(engine: FakeEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    async def main() -> None:
        db = await embedded.start()
        real = os.getpid()
        monkeypatch.setattr(embedded_module.os, "getpid", lambda: real + 1)
        db.abandon()
        monkeypatch.undo()
        assert not db.closed
        await db.close()

    run(main)
    assert not engine.started[0].abandoned


def test_a_close_does_not_wait_for_a_blocked_engine_on_the_loop_threads_shutdown(engine: FakeEngine) -> None:
    # `close` runs in a daemon thread of its own, so asyncio.run's executor shutdown never waits for it
    release = threading.Event()

    async def main() -> None:
        db = await embedded.start()
        engine.started[0].close_gate = release
        task = asyncio.ensure_future(db.close())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    started = time.monotonic()
    run(main)
    release.set()
    assert time.monotonic() - started < WAIT_SECONDS / 2

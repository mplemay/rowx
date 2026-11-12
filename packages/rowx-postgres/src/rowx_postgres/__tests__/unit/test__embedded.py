import asyncio
import os
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
import threading
import time

import pytest
from rowx_core.errors import InterfaceError
from rowx_postgres import (
    ConnectOptions,
    Pool,
    _connection as connection_module,
    _embedded as embedded_module,
    connect,
)
from rowx_postgres.__tests__.support.fake_engine import (
    SOCKET_DSN,
    WAIT_SECONDS,
    FakeConnection,
    FakeDirectoryInUseError,
    FakeEngine,
    FakeEngineError,
    FakeInstance,
    FakeSnapshotError,
    fork_and_run,
    run,
)
from rowx_postgres._admin import with_database
from rowx_postgres._embedded import (
    EmbeddedTarget,
    check_directory,
    parse_embedded_url,
    reject_embedded,
    start_owned,
)
from rowx_postgres._services import PostgresAdmin, PostgresMigrations, PostgresProvisioner, open_checker
from rowx_postgres.embedded import Database

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
    assert parse_embedded_url(url)[0] == EmbeddedTarget(directory=directory)


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
        parse_embedded_url(url)


@pytest.mark.parametrize("url", ["embedded:relative/db", "embedded:db", "embedded:./db"])
def test_a_relative_path_is_a_value_error(url: str) -> None:
    with pytest.raises(ValueError, match="absolute"):
        parse_embedded_url(url)


@pytest.mark.parametrize("name", ["sslmode", "SSLMODE", "host", "hostaddr", "port", "user", "password"])
def test_a_reserved_query_parameter_is_a_value_error_that_never_echoes_the_value(name: str) -> None:
    with pytest.raises(ValueError, match=name) as caught:
        parse_embedded_url(f"embedded://?{name}=Zq9value")
    assert "Zq9value" not in str(caught.value)


def test_a_parse_of_a_url_that_is_not_embedded_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="expected"):
        parse_embedded_url("postgresql://h/db")


# --- the engine load ---------------------------------------------------------------------------------------------


def test_a_missing_companion_is_an_interface_error_with_the_install_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "rowx_embedded", None)
    monkeypatch.setattr(embedded_module, "current_platform", lambda: ("linux", "x86_64"))
    embedded_module.load_cached.cache_clear()
    with pytest.raises(InterfaceError) as caught:
        run(Database().open)
    assert 'help: uv add "rowx[embedded]"' in str(caught.value)
    with pytest.raises(InterfaceError, match=r'uv add "rowx\[embedded\]"'):
        run(lambda: start_owned(None))


def test_an_unsupported_platform_is_an_interface_error_without_the_install_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "rowx_embedded", None)
    monkeypatch.setattr(embedded_module, "current_platform", lambda: ("win32", "amd64"))
    embedded_module.load_cached.cache_clear()
    with pytest.raises(InterfaceError, match="not available on this platform") as caught:
        run(Database().open)
    assert "uv add" not in str(caught.value)


def test_an_api_version_mismatch_names_both_versions(engine: FakeEngine) -> None:
    engine.API_VERSION = 99
    with pytest.raises(InterfaceError) as caught:
        run(lambda: start_owned(None))
    message = str(caught.value)
    assert "99" in message
    assert str(embedded_module.EXPECTED_API_VERSION) in message
    assert engine.started == []


def test_a_directory_in_use_is_an_interface_error_with_the_engine_message(engine: FakeEngine) -> None:
    engine.failure = FakeDirectoryInUseError("/var/lib/db is in use by pid 42")
    with pytest.raises(InterfaceError, match="is in use by pid 42"):
        run(lambda: start_owned("/var/lib/db"))
    engine.failure = FakeDirectoryInUseError("/var/lib/db is in use by another instance")
    with pytest.raises(InterfaceError, match="in use by another instance"):
        run(Database("/var/lib/db").open)


def test_a_relative_directory_is_a_value_error(engine: FakeEngine) -> None:
    with pytest.raises(ValueError, match="absolute"):
        check_directory("relative/db")
    assert engine.started == []


def test_start_returns_a_database_that_closes_once(engine: FakeEngine) -> None:
    async def main() -> str:
        async with await start_owned("/var/lib/db") as db:
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
        run(lambda: start_owned(None))
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


def test_a_database_used_in_a_forked_child_raises_and_its_close_leaves_the_parents_instance(
    engine: FakeEngine,
) -> None:
    db = run(lambda: start_owned(None))

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
    run(lambda: start_owned(None))

    def child() -> int:
        try:
            asyncio.run(start_owned(None))
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
    assert parse_embedded_url(url)[0] == EmbeddedTarget(directory=directory)


@pytest.mark.parametrize("url", ["embedded:///", "embedded:////", "embedded:///.", "embedded:///a/../.."])
def test_the_root_is_not_a_database_directory(url: str) -> None:
    with pytest.raises(ValueError, match="root"):
        parse_embedded_url(url)


@pytest.mark.parametrize("url", ["embedded://#frag", "embedded:///var/lib/db#x", "embedded://?a=1#x"])
def test_a_fragment_is_a_value_error(url: str) -> None:
    with pytest.raises(ValueError, match="fragment"):
        parse_embedded_url(url)


def test_a_duplicate_query_key_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="application_name"):
        parse_embedded_url("embedded://?application_name=a&application_name=b")


def test_a_path_with_a_semicolon_is_kept_whole() -> None:
    assert parse_embedded_url("embedded:///var/db;v1")[0] == EmbeddedTarget(directory="/var/db;v1")


def test_check_directory_normalises_and_refuses_the_root() -> None:
    assert check_directory("/var//lib/db/") == "/var/lib/db"
    with pytest.raises(ValueError, match="root"):
        check_directory("/")


@pytest.mark.parametrize("url", ["embedded://", "embedded:///var/lib/db", "EMBEDDED://?application_name=x"])
def test_generic_connect_pool_and_from_dsn_reject_an_embedded_url_with_a_pointer(url: str) -> None:
    for build in (lambda: run(lambda: connect(url)), lambda: Pool(url), lambda: ConnectOptions.from_dsn(url)):
        with pytest.raises(InterfaceError, match=r"use rowx\.postgres\.embedded\.Database\(directory\)"):
            build()


def test_the_repr_of_ordinary_options_has_no_embedded_part() -> None:
    shown = repr(ConnectOptions(host="h", extra={"sslpassword": "x"}))
    assert "embedded" not in shown
    assert shown.endswith("extra={'sslpassword': '<redacted>'})")


@pytest.mark.parametrize("target", ["embedded://", "embedded:///var/lib/db", parse_embedded_url("embedded://")])
def test_every_server_only_path_rejects_an_embedded_target_clearly(target: str | ConnectOptions) -> None:
    dsn = target if isinstance(target, str) else "embedded://"
    for build in (
        lambda: PostgresAdmin(dsn),
        lambda: PostgresProvisioner(dsn),
        lambda: with_database(dsn, "other"),
        lambda: reject_embedded(dsn),
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


def test_an_unexpected_engine_failure_becomes_an_interface_error_with_its_cause(engine: FakeEngine) -> None:
    engine.failure = RuntimeError("wasm trap")
    with pytest.raises(InterfaceError, match="wasm trap") as caught:
        run(lambda: start_owned(None))
    assert isinstance(caught.value.__cause__, RuntimeError)


def test_concurrent_closes_close_once_and_the_second_waits_for_the_first(engine: FakeEngine) -> None:
    release = threading.Event()
    original = FakeInstance.close

    def slow_close(self: FakeInstance) -> None:
        self.entered_close.set()
        assert release.wait(WAIT_SECONDS)
        original(self)

    db = run(lambda: start_owned(None))
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
    db = run(lambda: start_owned(None))
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
        db = await start_owned(None)
        db.abandon()
        assert db.closed
        # idempotent with close, which then has nothing to do
        db.abandon()
        await db.close()

    run(main)
    assert (engine.started[0].abandoned, engine.started[0].closes) == (True, 0)


def test_abandon_refuses_a_directory_unless_forced(engine: FakeEngine) -> None:
    async def main() -> None:
        db = await start_owned("/var/lib/db")
        with pytest.raises(InterfaceError, match="cannot be abandoned"):
            db.abandon()
        assert not db.closed
        db.abandon(force=True)
        assert db.closed

    run(main)
    assert engine.started[0].abandoned


def test_abandon_after_a_close_does_nothing(engine: FakeEngine) -> None:
    async def main() -> None:
        db = await start_owned(None)
        await db.close()
        db.abandon()

    run(main)
    assert (engine.started[0].abandoned, engine.started[0].closes) == (False, 1)


def test_abandon_in_a_forked_child_is_a_no_op(engine: FakeEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    async def main() -> None:
        db = await start_owned(None)
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
        db = await start_owned(None)
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


@pytest.mark.parametrize(
    "statement",
    ["import rowx", "import rowx_postgres", "import rowx_postgres.embedded", "import rowx.postgres.embedded"],
)
def test_importing_never_imports_the_companion(statement: str) -> None:
    code = f"import sys\n{statement}\nassert 'rowx_embedded' not in sys.modules\n"
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

import asyncio
import os
import signal
import sys
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Final, Self, cast

import pytest
from rowx_core.backend import Backend, CheckerBackend, EmbeddedService, ScratchBuild, ScratchService
from rowx_core.errors import DatabaseError, InterfaceError, MigrateError
from rowx_postgres._compiler import COMPILER

from rowx._backend import LOADED, SCHEMES
from rowx.check.cli import main
from rowx.check.embedded import embedded_for, open_instance, scratch_session, socket_directory
from rowx.check.runner import CheckError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Sequence
    from contextlib import AbstractAsyncContextManager
    from pathlib import Path
    from types import TracebackType

    from rowx_core.backend import (
        CheckerSession,
        MigrationResult,
        MigrationService,
        MigrationStatus,
        ShapeIssue,
    )

SOCKET: Final[str] = "postgresql://postgres@/postgres?host=/run/fake-embedded&sslmode=disable"
THREE_QUERIES: Final[str] = (
    "from rowx import query\n\n"
    'query[tuple[int]](t"SELECT 1")\n'
    'query[tuple[int]](t"SELECT 2")\n'
    'query[tuple[int]](t"SELECT 3")\n'
)
QUERY: Final[str] = 'from rowx import query\n\nquery[tuple[int]](t"SELECT 1")\n'


def unused_mapping(*_args: object) -> tuple[Sequence[int | None], Sequence[ShapeIssue]]:
    message = "the checker is never reached"
    raise AssertionError(message)


def unused_connect(_dsn: str, _timeout: float) -> Awaitable[CheckerSession]:
    message = "the checker is never reached"
    raise AssertionError(message)


@dataclass(slots=True, kw_only=True)
class FakeSession:
    calls: list[str]

    async def run(self, *, target: int | None = None, dry_run: bool = False) -> Sequence[MigrationResult]:
        del target, dry_run
        self.calls.append("run")
        return ()

    async def info(self) -> Sequence[MigrationStatus]:
        self.calls.append("info")
        return ()

    async def close(self) -> None:
        self.calls.append("close")

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.close()


@dataclass(slots=True, kw_only=True)
class FakeMigrator:
    events: list[str]
    dsns: list[str] = field(default_factory=list)

    @property
    def migrations(self) -> tuple[()]:
        return ()

    async def open(self, **_options: object) -> FakeSession:
        self.events.append("migrator.open")
        return FakeSession(calls=self.events)

    async def run_on(self, dsn: str) -> tuple[()]:
        self.dsns.append(dsn)
        return ()


@dataclass(slots=True, kw_only=True)
class FakeScratch:
    events: list[str]
    error: Exception | None = None
    swept: bool = False
    workers: list[int] = field(default_factory=list)
    urls: tuple[str, ...] = ()

    def session(self, migrator: MigrationService, workers: int) -> AbstractAsyncContextManager[ScratchBuild]:
        del migrator
        return self._session(workers)

    @asynccontextmanager
    async def _session(self, workers: int) -> AsyncGenerator[ScratchBuild]:
        self.events.append("scratch.start")
        self.workers.append(workers)
        if self.error is not None:
            raise self.error
        # one URL per worker, as an embedded scratch makes them
        self.urls = tuple(f"{SOCKET}&n={n}" for n in range(workers))
        try:
            yield ScratchBuild(urls=self.urls, seconds=0.0)
        finally:
            self.events.append("scratch.close")

    def sweep(self) -> None:
        self.swept = True


@dataclass(slots=True, kw_only=True)
class FakeEmbedded:
    events: list[str]
    persistent: bool
    open_error: Exception | None = None
    # makes the close wait, as a real one does behind a running statement, for at most this many seconds
    close_wait: float = 0.0
    scratches: list[FakeScratch] = field(default_factory=list)
    swept: int = 0

    def open(self) -> AbstractAsyncContextManager[str]:
        return self._open()

    @asynccontextmanager
    async def _open(self) -> AsyncGenerator[str]:
        self.events.append("open")
        if self.open_error is not None:
            raise self.open_error
        try:
            yield SOCKET
        finally:
            if self.close_wait:
                # cancelled by the abort, which leaves the loop
                await asyncio.sleep(self.close_wait)
            self.events.append("close")

    def scratch(self) -> ScratchService:
        made = FakeScratch(events=self.events)
        self.scratches.append(made)
        return made

    def sweep(self) -> None:
        self.swept += 1


@dataclass(slots=True, kw_only=True)
class World:
    events: list[str]
    embedded: FakeEmbedded
    migrator: FakeMigrator
    described: list[Sequence[str]]
    gated: list[str]


def install(monkeypatch: pytest.MonkeyPatch, *, persistent: bool) -> World:
    events: list[str] = []
    embedded = FakeEmbedded(events=events, persistent=persistent)
    migrator = FakeMigrator(events=events)

    def service(url: str) -> EmbeddedService | None:
        return embedded if url.startswith("fakedb-embedded:") else None

    def checker() -> CheckerBackend:
        return CheckerBackend(
            connect_checker=unused_connect,
            compiler=COMPILER,
            map_columns=unused_mapping,
            types={},
            param_classes={},
            value_leaves={},
            range_leaves={},
            range_classes=frozenset(),
            value_classes={},
        )

    def migrations(_dsn: str, _path: object, **_options: object) -> MigrationService:
        return cast("MigrationService", migrator)

    backend = Backend(checker_factory=checker, embedded_factory=service, migration_factory=migrations)
    monkeypatch.setitem(sys.modules, "fake_embedded_backend", SimpleNamespace(load=lambda: backend))
    monkeypatch.setitem(SCHEMES, "fakedb-embedded", ("fake_embedded_backend", "fake"))
    monkeypatch.delitem(LOADED, "fake_embedded_backend", raising=False)

    described: list[Sequence[str]] = []
    gated: list[str] = []

    async def describe_all(urls: Sequence[str], statements: list[str], *_args: object) -> dict[str, DatabaseError]:  # ruff: ignore[unused-async]
        events.append("describe")
        described.append(urls)
        return {sql: DatabaseError("42P01", "relation does not exist") for sql in statements}

    async def check_drift(_config: object, url: str, _backend: object = None) -> int:  # ruff: ignore[unused-async]
        events.append("gate")
        gated.append(url)
        return 1

    monkeypatch.setattr("rowx.check.runner.describe_all", describe_all)
    monkeypatch.setattr("rowx.check.drift.check_drift", check_drift)
    return World(events=events, embedded=embedded, migrator=migrator, described=described, gated=gated)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    (tmp_path / "pyproject.toml").write_text("[tool.rowx.migrate]\n", encoding="utf-8")
    (tmp_path / "migrations").mkdir()
    (tmp_path / "q.py").write_text(QUERY, encoding="utf-8")
    return tmp_path


def run(*argv: str) -> int:
    return main(argv=list(argv))


def test_a_memory_url_implies_scratch_skips_the_gate_and_describes_one_url_per_worker(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = install(monkeypatch, persistent=False)
    (project / "q.py").write_text(THREE_QUERIES, encoding="utf-8")
    assert run("check", "q.py", "--database-url", "fakedb-embedded://", "-j", "2") == 1
    assert world.embedded.scratches[0].workers == [2]
    assert world.described == [world.embedded.scratches[0].urls]
    assert len(world.described[0]) == 2
    assert world.gated == []
    assert world.events == ["scratch.start", "describe", "scratch.close"]
    assert "sql-error" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("jobs", "statements", "expected"), [("8", THREE_QUERIES, 3), ("2", THREE_QUERIES, 2), ("8", QUERY, 1)]
)
def test_a_scratch_is_built_for_no_more_workers_than_statements(
    jobs: str, statements: str, expected: int, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (project / "q.py").write_text(statements, encoding="utf-8")
    world = install(monkeypatch, persistent=False)
    assert run("check", "q.py", "--database-url", "fakedb-embedded://", "-j", jobs) == 1
    assert world.embedded.scratches[0].workers == [expected]
    assert len(world.described[0]) == expected


def test_a_memory_url_without_migrate_config_exits_two_before_anything_starts(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "pyproject.toml").write_text("", encoding="utf-8")
    world = install(monkeypatch, persistent=False)
    assert run("check", "q.py", "--database-url", "fakedb-embedded://") == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert "[tool.rowx.migrate]" in captured.err
    assert world.events == []


@pytest.mark.usefixtures("project")
def test_a_memory_migration_failure_keeps_the_server_scratch_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = install(monkeypatch, persistent=False)
    cause = DatabaseError("22012", "division by zero")
    failure = MigrateError("migration 2 failed")
    failure.__cause__ = cause
    monkeypatch.setattr(
        FakeEmbedded,
        "scratch",
        lambda self: FakeScratch(events=self.events, error=failure),
    )
    assert run("check", "q.py", "--database-url", "fakedb-embedded://") == 2
    err = capsys.readouterr().err
    assert err == "error: the migrations failed on the scratch database: migration 2 failed: 22012: division by zero\n"
    assert world.described == []


@pytest.mark.usefixtures("project")
def test_an_interrupted_memory_run_still_sweeps_the_scratch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = install(monkeypatch, persistent=False)

    async def interrupted(*_args: object) -> None:  # ruff: ignore[unused-async]
        raise KeyboardInterrupt

    monkeypatch.setattr("rowx.check.runner.describe_all", interrupted)
    assert run("check", "q.py", "--database-url", "fakedb-embedded://") == 2
    assert "interrupted" in capsys.readouterr().err
    assert world.embedded.scratches[0].swept
    assert world.events == ["scratch.start", "scratch.close"]


@pytest.mark.usefixtures("project")
def test_a_directory_starts_once_and_hands_its_socket_dsn_to_the_gate_and_describe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = install(monkeypatch, persistent=True)
    assert run("check", "q.py", "--database-url", "fakedb-embedded:///var/db", "-j", "4") == 1
    # -j 4 is clamped to the one session that the instance serves
    assert world.described == [(SOCKET,)]
    assert world.gated == [SOCKET]
    assert world.events == ["open", "gate", "describe", "close"]
    assert world.embedded.scratches == []


@pytest.mark.usefixtures("project")
def test_a_directory_with_no_drift_check_skips_only_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    world = install(monkeypatch, persistent=True)
    assert run("check", "q.py", "--database-url", "fakedb-embedded:///var/db", "--no-drift-check") == 1
    assert world.events == ["open", "describe", "close"]


def test_a_directory_without_migrate_config_has_no_gate(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (project / "pyproject.toml").write_text("", encoding="utf-8")
    world = install(monkeypatch, persistent=True)
    assert run("check", "q.py", "--database-url", "fakedb-embedded:///var/db") == 1
    assert world.events == ["open", "describe", "close"]


@pytest.mark.usefixtures("project")
def test_a_directory_with_scratch_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = install(monkeypatch, persistent=True)
    assert run("check", "q.py", "--database-url", "fakedb-embedded:///var/db", "--scratch") == 2
    assert "directory" in capsys.readouterr().err
    assert world.events == []


@pytest.mark.usefixtures("project")
def test_a_directory_that_cannot_start_exits_two_without_describing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = install(monkeypatch, persistent=True)
    world.embedded.open_error = InterfaceError("the directory is in use by pid 7")
    assert run("check", "q.py", "--database-url", "fakedb-embedded:///var/db") == 2
    assert capsys.readouterr().err == "error: could not start the embedded database: the directory is in use by pid 7\n"
    assert world.events == ["open"]


@pytest.mark.usefixtures("project")
def test_the_socket_dsn_is_redacted_from_a_failure_while_describing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    install(monkeypatch, persistent=True)

    async def failing(urls: Sequence[str], *_args: object) -> None:  # ruff: ignore[unused-async]
        msg = f"could not connect to {urls[0]}"
        raise CheckError(msg)

    monkeypatch.setattr("rowx.check.runner.describe_all", failing)
    assert run("check", "q.py", "--database-url", "fakedb-embedded:///var/db") == 2
    err = capsys.readouterr().err
    assert err == "error: could not connect to <database url>\n"


def test_nothing_starts_when_there_is_nothing_to_describe(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (project / "q.py").write_text("x = 1\n", encoding="utf-8")
    world = install(monkeypatch, persistent=False)
    assert run("check", "q.py", "--database-url", "fakedb-embedded://") == 0
    assert world.events == []


@pytest.mark.usefixtures("project")
def test_migrate_on_a_directory_starts_the_instance_once_around_the_command(monkeypatch: pytest.MonkeyPatch) -> None:
    world = install(monkeypatch, persistent=True)
    assert run("migrate", "run", "--database-url", "fakedb-embedded:///var/db") == 0
    assert world.events == ["open", "migrator.open", "run", "close", "close"]


@pytest.mark.usefixtures("project")
def test_migrate_on_a_directory_that_cannot_start_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = install(monkeypatch, persistent=True)
    world.embedded.open_error = InterfaceError("the directory is in use by pid 7")
    assert run("migrate", "info", "--database-url", "fakedb-embedded:///var/db", "--connect-timeout", "30") == 2
    assert capsys.readouterr().err == "error: could not start the embedded database: the directory is in use by pid 7\n"
    assert world.events == ["open"]


@pytest.mark.usefixtures("project")
def test_migrate_on_a_memory_url_exits_two_before_anything_starts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = install(monkeypatch, persistent=False)
    assert run("migrate", "run", "--database-url", "fakedb-embedded://") == 2
    assert "an in-memory embedded database is gone when the command exits" in capsys.readouterr().err
    assert world.events == []


@pytest.mark.parametrize("persistent", [True, False])
@pytest.mark.usefixtures("project")
def test_database_commands_exit_two_on_any_embedded_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *, persistent: bool
) -> None:
    world = install(monkeypatch, persistent=persistent)
    url = "fakedb-embedded:///var/db" if persistent else "fakedb-embedded://"
    assert run("database", "create", "--database-url", url) == 2
    assert capsys.readouterr().err == "error: `rowx database` is not supported for embedded URLs\n"
    assert world.events == []


def test_embedded_for_is_none_for_everything_that_is_not_an_embedded_url(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, persistent=True)
    assert embedded_for(None) is None
    assert embedded_for("  ") is None
    assert embedded_for("sqlite://x") is None
    assert embedded_for("fakedb-embedded:///var/db") is not None


def test_a_missing_backend_is_left_for_the_code_that_needs_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(SCHEMES, "fakedb-missing", ("no_such_backend_module", "fake"))
    assert embedded_for("fakedb-missing://x") is None


def test_an_unparsable_embedded_url_never_quotes_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(_url: str) -> EmbeddedService | None:
        msg = "unparsable: fakedb-embedded://user:hunter2secret@host"
        raise ValueError(msg)

    monkeypatch.setitem(
        sys.modules, "fake_refusing_backend", SimpleNamespace(load=lambda: Backend(embedded_factory=refuse))
    )
    monkeypatch.setitem(SCHEMES, "fakedb-refusing", ("fake_refusing_backend", "fake"))
    monkeypatch.delitem(LOADED, "fake_refusing_backend", raising=False)
    with pytest.raises(CheckError) as caught:
        embedded_for("fakedb-refusing://user:hunter2secret@host")
    assert str(caught.value) == "the database URL could not be parsed"


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("embedded://user:hunter2secret@host/db", "no host, port, user or password"),
        ("embedded://?password=hunter2secret", "password cannot be set"),
        ("embedded://#hunter2secret", "no fragment"),
        ("embedded://[hunter2secret", "could not be parsed"),
    ],
)
def test_an_embedded_url_the_backend_refuses_gives_the_reason_without_its_values(url: str, reason: str) -> None:
    with pytest.raises(CheckError) as caught:
        embedded_for(url)
    assert reason in str(caught.value)
    assert "hunter2secret" not in str(caught.value)


def test_a_relative_embedded_directory_names_the_rule() -> None:
    with pytest.raises(CheckError, match="an embedded URL has no host"):
        embedded_for("embedded://rel/path")
    with pytest.raises(CheckError, match="must be an absolute path"):
        embedded_for("embedded:rel/path")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (InterfaceError("no engine"), "could not build the embedded scratch database: no engine"),
        (DatabaseError("57P01", "gone"), "could not build the embedded scratch database: 57P01: gone"),
        (OSError("disk full"), "could not build the embedded scratch database: disk full"),
    ],
)
def test_scratch_session_maps_backend_errors(error: Exception, expected: str) -> None:
    import asyncio  # ruff: ignore[import-outside-top-level]

    async def go() -> None:
        scratch = FakeScratch(events=[], error=error)
        async with scratch_session(scratch, cast("MigrationService", FakeMigrator(events=[])), 1):
            pass

    with pytest.raises(CheckError) as caught:
        asyncio.run(go())
    assert str(caught.value) == expected


def test_scratch_session_does_not_hide_an_error_from_its_body() -> None:
    import asyncio  # ruff: ignore[import-outside-top-level]

    async def go() -> None:
        scratch = FakeScratch(events=[])
        async with scratch_session(scratch, cast("MigrationService", FakeMigrator(events=[])), 1):
            msg = "from the body"
            raise InterfaceError(msg)

    with pytest.raises(InterfaceError, match="from the body"):
        asyncio.run(go())


def test_open_instance_does_not_hide_an_error_from_its_body() -> None:
    import asyncio  # ruff: ignore[import-outside-top-level]

    async def go() -> None:
        async with open_instance(FakeEmbedded(events=[], persistent=True)):
            msg = "from the body"
            raise OSError(msg)

    with pytest.raises(OSError, match="from the body"):
        asyncio.run(go())


@pytest.mark.usefixtures("project")
def test_a_message_that_quotes_only_the_socket_directory_is_redacted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    install(monkeypatch, persistent=True)

    async def failing(urls: Sequence[str], *_args: object) -> None:  # ruff: ignore[unused-async]
        del urls
        msg = "could not connect to /run/fake-embedded/.s.PGSQL.5432: refused"
        raise CheckError(msg)

    monkeypatch.setattr("rowx.check.runner.describe_all", failing)
    assert run("check", "q.py", "--database-url", "fakedb-embedded:///var/db") == 2
    assert capsys.readouterr().err == "error: could not connect to <socket directory>/.s.PGSQL.5432: refused\n"


def test_the_socket_directory_of_an_url_is_found_only_when_it_is_a_path() -> None:
    assert socket_directory(SOCKET) == "/run/fake-embedded"
    assert socket_directory("postgresql://h/db?host=db.example") is None
    assert socket_directory("postgresql://h/db") is None
    assert socket_directory("postgresql://[bad") is None


@pytest.mark.usefixtures("project")
def test_an_interrupted_directory_run_sweeps_its_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    world = install(monkeypatch, persistent=True)

    async def interrupted(*_args: object) -> None:  # ruff: ignore[unused-async]
        raise KeyboardInterrupt

    monkeypatch.setattr("rowx.check.runner.describe_all", interrupted)
    assert run("check", "q.py", "--database-url", "fakedb-embedded:///var/db") == 2
    assert world.embedded.swept == 1


def interrupt_in(seconds: float) -> threading.Timer:
    timer = threading.Timer(seconds, os.kill, (os.getpid(), signal.SIGINT))
    timer.start()
    return timer


@pytest.mark.usefixtures("project")
def test_the_first_ctrl_c_on_a_directory_says_it_is_waiting_and_exits_two_once_the_instance_closes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = install(monkeypatch, persistent=True)

    async def running(*_args: object) -> None:
        # a statement that the first Ctrl-C cannot stop
        os.kill(os.getpid(), signal.SIGINT)
        await asyncio.sleep(30)

    monkeypatch.setattr("rowx.check.runner.describe_all", running)
    assert run("check", "q.py", "--database-url", "fakedb-embedded:///var/db") == 2
    err = capsys.readouterr().err
    assert err.startswith("interrupted: waiting for the running statement to finish")
    assert "press Ctrl-C again to abort" in err
    assert err.endswith("error: interrupted\n")
    # the close ran to the end (the teardown's sweep of a closed instance is a no-op)
    assert world.events == ["open", "gate", "close"]


@pytest.mark.usefixtures("project")
def test_the_second_ctrl_c_on_a_directory_stops_the_instance_without_waiting_for_the_close(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = install(monkeypatch, persistent=True)
    world.embedded.close_wait = 30.0

    async def running(*_args: object) -> None:
        os.kill(os.getpid(), signal.SIGINT)
        await asyncio.sleep(30)

    monkeypatch.setattr("rowx.check.runner.describe_all", running)
    timer = interrupt_in(0.5)
    started = time.monotonic()
    try:
        assert run("check", "q.py", "--database-url", "fakedb-embedded:///var/db") == 2
    finally:
        timer.cancel()
    assert time.monotonic() - started < 10
    # by the abort, and again by the run's own teardown
    assert world.embedded.swept >= 1
    assert "close" not in world.events
    assert capsys.readouterr().err.endswith("error: interrupted\n")

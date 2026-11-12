import asyncio
import sys
import threading
from types import ModuleType
from typing import TYPE_CHECKING, Any, Final, cast

import pytest
from rowx_core.errors import InterfaceError, MigrateError
from rowx_postgres import _embedded as embedded_module
from rowx_postgres._scratch import EmbeddedScratch, PostgresEmbedded, embedded_service, socket_url

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from rowx_core.backend import MigrationService

SOCKET: Final[str] = "postgresql://postgres@/postgres?host=/nonexistent/rowx-{n}&sslmode=disable"


class FakeEngineError(Exception):
    pass


class FakeSnapshotError(FakeEngineError):
    pass


class FakeInstance:
    def __init__(self, engine: FakeEngine, directory: str | None, snapshot: bytes | None) -> None:
        self.directory = directory
        self.booted_from = snapshot
        self.dsn = SOCKET.format(n=len(engine.started))
        self.closes = 0
        self.fail_closes = 0
        self.closed = False
        self.abandoned = False
        self.abandon_error: Exception | None = None
        self.taken = 0

    def snapshot(self) -> bytes:
        self.taken += 1
        return b"snapshot"

    def abandon(self, *, force: bool = False) -> None:
        if self.abandon_error is not None:
            raise self.abandon_error
        if self.directory is not None and not force:
            msg = "a persistent database cannot be abandoned without force; close it"
            raise FakeEngineError(msg)
        self.abandoned = True
        self.closed = True

    def close(self) -> None:
        self.closes += 1
        if self.fail_closes > 0:
            self.fail_closes -= 1
            msg = "the engine thread is stuck"
            raise FakeEngineError(msg)
        self.closed = True


class FakeEngine(ModuleType):
    def __init__(self) -> None:
        super().__init__("rowx_embedded")
        self.API_VERSION = embedded_module.EXPECTED_API_VERSION
        self.EngineError = FakeEngineError
        self.SnapshotError = FakeSnapshotError
        self.Instance = self
        self.started: list[FakeInstance] = []
        # the 1-based start numbers that fail
        self.failing: set[int] = set()
        self.attempts = 0
        self.lock = threading.Lock()

    def start(self, directory: str | None = None, snapshot: bytes | None = None) -> FakeInstance:
        with self.lock:
            self.attempts += 1
            number = self.attempts
            if number in self.failing:
                msg = f"start {number} failed"
                raise FakeEngineError(msg)
            instance = FakeInstance(self, directory, snapshot)
            self.started.append(instance)
            return instance


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeEngine]:
    fake = FakeEngine()
    monkeypatch.setitem(sys.modules, "rowx_embedded", fake)
    embedded_module.load_cached.cache_clear()
    yield fake
    embedded_module.load_cached.cache_clear()


class FakeMigrator:
    def __init__(self) -> None:
        self.dsns: list[str] = []
        self.error: Exception | None = None
        self.entered = asyncio.Event()
        self.hold = False

    async def run_on(self, dsn: str) -> tuple[()]:
        self.dsns.append(dsn)
        self.entered.set()
        if self.hold:
            await asyncio.Event().wait()
        if self.error is not None:
            raise self.error
        return ()


def run[T](factory: Callable[[], Any]) -> T:
    return asyncio.run(factory())


def build(scratch: EmbeddedScratch, migrator: FakeMigrator, workers: int = 1) -> tuple[str, ...]:
    async def go() -> tuple[str, ...]:
        async with scratch.session(cast("MigrationService", migrator), workers) as built:
            return built.urls

    return run(go)


def test_one_worker_migrates_its_instance_in_place_and_takes_no_snapshot(engine: FakeEngine) -> None:
    migrator = FakeMigrator()
    urls = build(EmbeddedScratch(), migrator, 1)
    assert len(engine.started) == 1
    (only,) = engine.started
    assert urls == (only.dsn,)
    assert migrator.dsns == [only.dsn]
    assert only.taken == 0
    assert only.closed


def test_several_workers_boot_from_one_snapshot_of_the_migrated_instance(engine: FakeEngine) -> None:
    migrator = FakeMigrator()
    urls = build(EmbeddedScratch(), migrator, 4)
    first, *others = engine.started
    assert len(urls) == 4
    assert len(set(urls)) == 4
    assert migrator.dsns == [first.dsn]
    assert first.taken == 1
    assert [instance.booted_from for instance in others] == [b"snapshot"] * 3
    assert all(instance.closed for instance in engine.started)


def test_a_failed_migration_closes_the_instance_and_propagates(engine: FakeEngine) -> None:
    migrator = FakeMigrator()
    migrator.error = MigrateError("migration 2 failed")
    with pytest.raises(MigrateError, match="migration 2 failed"):
        build(EmbeddedScratch(), migrator, 4)
    assert len(engine.started) == 1
    assert engine.started[0].closed


def test_a_failed_boot_closes_the_instances_that_did_start(engine: FakeEngine) -> None:
    engine.failing = {3}
    with pytest.raises(InterfaceError, match="start 3 failed"):
        build(EmbeddedScratch(), FakeMigrator(), 4)
    assert len(engine.started) == 3
    assert all(instance.closed for instance in engine.started)


@pytest.mark.usefixtures("engine")
def test_the_query_of_the_url_reaches_every_worker_and_the_migrator() -> None:
    migrator = FakeMigrator()
    urls = build(EmbeddedScratch("application_name=checker"), migrator, 2)
    assert all(url.endswith("&application_name=checker") for url in urls)
    assert migrator.dsns == [urls[0]]


def test_a_cancelled_migration_abandons_every_instance_instead_of_closing_it(engine: FakeEngine) -> None:
    migrator = FakeMigrator()
    migrator.hold = True
    scratch = EmbeddedScratch()

    async def go() -> None:
        task = asyncio.create_task(build_async(scratch, migrator))
        await migrator.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def build_async(target: EmbeddedScratch, service: FakeMigrator) -> None:
        async with target.session(cast("MigrationService", service), 2):
            pass

    run(go)
    assert [(instance.abandoned, instance.closes) for instance in engine.started] == [(True, 0)]
    assert not scratch.leaked


def test_an_interrupt_in_the_body_abandons_the_workers_too(engine: FakeEngine) -> None:
    scratch = EmbeddedScratch()

    async def go() -> None:
        async with scratch.session(cast("MigrationService", FakeMigrator()), 3):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        run(go)
    assert [(instance.abandoned, instance.closes) for instance in engine.started] == [(True, 0)] * 3
    assert not scratch.leaked


def test_an_ordinary_error_in_the_body_closes_the_workers(engine: FakeEngine) -> None:
    scratch = EmbeddedScratch()

    async def go() -> None:
        async with scratch.session(cast("MigrationService", FakeMigrator()), 2):
            msg = "describe failed"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="describe failed"):
        run(go)
    assert [(instance.abandoned, instance.closes) for instance in engine.started] == [(False, 1)] * 2


@pytest.mark.parametrize("workers", [1, 2, 5])
def test_exactly_as_many_instances_are_booted_as_there_are_workers(engine: FakeEngine, workers: int) -> None:
    assert len(build(EmbeddedScratch(), FakeMigrator(), workers)) == workers
    assert engine.attempts == workers
    # only the first is a fresh boot; every other worker comes from the snapshot
    assert [instance.booted_from is not None for instance in engine.started] == [False, *[True] * (workers - 1)]


def test_a_close_that_failed_is_left_to_the_sweep_which_abandons(
    engine: FakeEngine, capsys: pytest.CaptureFixture[str]
) -> None:
    scratch = EmbeddedScratch()

    async def go() -> None:
        async with scratch.session(cast("MigrationService", FakeMigrator()), 1):
            engine.started[0].fail_closes = 1

    run(go)
    assert scratch.leaked
    assert scratch.close_error == "the engine thread is stuck"
    scratch.sweep()
    assert not scratch.leaked
    # abandoned, not closed again: a second close would wait on the same stuck engine
    assert engine.started[0].abandoned
    assert engine.started[0].closes == 1
    assert not capsys.readouterr().err


def test_a_sweep_that_cannot_abandon_says_so(engine: FakeEngine, capsys: pytest.CaptureFixture[str]) -> None:
    scratch = EmbeddedScratch()

    async def go() -> None:
        async with scratch.session(cast("MigrationService", FakeMigrator()), 1):
            engine.started[0].fail_closes = 5
            engine.started[0].abandon_error = FakeEngineError("stuck")

    run(go)
    scratch.sweep()
    assert scratch.leaked
    assert "warning: could not stop the embedded scratch database" in capsys.readouterr().err


def test_a_sweep_with_nothing_open_does_nothing(engine: FakeEngine) -> None:
    EmbeddedScratch().sweep()
    assert engine.started == []


def test_a_scratch_needs_a_worker() -> None:
    with pytest.raises(ValueError, match="at least one worker"):
        EmbeddedScratch().session(cast("MigrationService", FakeMigrator()), 0)


def test_socket_url_keeps_the_socket_parameters_and_appends_the_rest() -> None:
    url = socket_url(SOCKET.format(n=0), "options=-c%20search_path%3Dapp&application_name=x")
    assert url.startswith("postgresql://postgres@/postgres?host=%2Fnonexistent%2Frowx-0&sslmode=disable&options=")
    assert url.endswith("&application_name=x")
    assert socket_url(SOCKET.format(n=0), "") == SOCKET.format(n=0)


def test_a_memory_service_is_not_persistent_and_refuses_to_open(engine: FakeEngine) -> None:
    service = PostgresEmbedded("embedded://")
    assert not service.persistent

    async def go() -> None:
        async with service.open():
            pass

    with pytest.raises(InterfaceError, match="gone when the command exits"):
        run(go)
    assert engine.started == []


def test_a_directory_service_starts_once_yields_the_socket_dsn_and_closes(engine: FakeEngine) -> None:
    service = PostgresEmbedded("embedded:///var/db?application_name=x")
    assert service.persistent
    seen: list[str] = []

    async def go() -> None:
        async with service.open() as url:
            seen.append(url)
            assert not engine.started[0].closed

    run(go)
    assert len(engine.started) == 1
    assert engine.started[0].directory == "/var/db"
    assert seen[0].endswith("&application_name=x")
    assert engine.started[0].closed


def test_a_directory_that_cannot_start_fails_at_once_without_retrying(engine: FakeEngine) -> None:
    engine.failing = {1}

    async def go() -> None:
        async with PostgresEmbedded("embedded:///var/db").open():
            pass

    with pytest.raises(InterfaceError, match="start 1 failed"):
        run(go)
    assert engine.started == []


@pytest.mark.usefixtures("engine")
def test_the_scratch_service_is_an_embedded_scratch() -> None:
    assert isinstance(PostgresEmbedded("embedded://").scratch(), EmbeddedScratch)


def test_the_factory_answers_only_for_an_embedded_url() -> None:
    assert embedded_service("postgresql://h/db") is None
    assert embedded_service("host=h dbname=db") is None
    assert embedded_service("embedded://") is not None
    with pytest.raises(ValueError, match="no host"):
        embedded_service("embedded://host")


def test_a_directory_service_sweeps_an_instance_whose_close_failed_with_force(engine: FakeEngine) -> None:
    service = PostgresEmbedded("embedded:///var/db")

    async def go() -> None:
        async with service.open():
            engine.started[0].fail_closes = 1

    with pytest.raises(FakeEngineError, match="stuck"):
        run(go)
    (instance,) = engine.started
    assert not instance.closed
    service.sweep()
    assert instance.abandoned
    # nothing to sweep once it is stopped, and a service that never started has nothing either
    service.sweep()
    PostgresEmbedded("embedded:///other").sweep()

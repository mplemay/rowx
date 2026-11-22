import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from rowx_core.backend import AdminService, Backend, MigrationService, ScratchBuild
from rowx_core.errors import InterfaceError, MigrateError

from rowx.check.runner import CheckError
from rowx.check.scratch import BackendScratch, Scratch, scratch_for

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


def test_scratch_names_fit_the_identifier_limit() -> None:
    names = {Scratch("postgresql://u@h/app", Mock(spec=AdminService)).name for _ in range(20)}
    assert len(names) == 20
    for name in names:
        assert name.startswith("rowx_check_")
        assert len(name) == len("rowx_check_") + 16
        int(name.removeprefix("rowx_check_"), 16)


@pytest.mark.parametrize(
    "dsn",
    ["postgresql://u:hunter2secret@h/app?sslmode", "host=h password=hunter2secret dbname='unterminated"],
)
def test_scratch_url_that_cannot_be_parsed_never_quotes_the_dsn(dsn: str) -> None:
    admin = Mock(spec=AdminService)
    admin.with_database.side_effect = InterfaceError("invalid target")
    with pytest.raises(CheckError) as caught:
        _ = Scratch(dsn, admin).url
    assert str(caught.value) == "the database URL could not be parsed"


def test_collision_does_not_drop_an_existing_database() -> None:
    import asyncio  # ruff: ignore[import-outside-top-level]
    from unittest.mock import AsyncMock  # ruff: ignore[import-outside-top-level]

    from rowx_core.errors import DatabaseError  # ruff: ignore[import-outside-top-level]

    admin = Mock(spec=AdminService)
    admin.with_database.return_value = "postgresql://host/scratch"
    admin.create_scratch = AsyncMock(side_effect=DatabaseError("42P04", "already exists"))
    admin.drop = AsyncMock()
    scratch = Scratch("postgresql://host/postgres", admin, sweep_previous=False)

    async def run() -> None:
        with pytest.raises(CheckError):
            async with scratch.session(Mock()):
                pass

    asyncio.run(run())
    admin.drop.assert_not_awaited()
    assert not scratch.leaked


def test_a_duplicate_answered_by_the_admin_keeps_the_existing_database() -> None:
    import asyncio  # ruff: ignore[import-outside-top-level]
    from unittest.mock import AsyncMock  # ruff: ignore[import-outside-top-level]

    from rowx_core.errors import DatabaseError  # ruff: ignore[import-outside-top-level]

    error = DatabaseError("HY000", "exists", code="SQLITE_EXAMPLE")
    admin = Mock(spec=AdminService)
    admin.with_database.return_value = "postgresql://host/scratch"
    admin.duplicate.return_value = True
    admin.create_scratch = AsyncMock(side_effect=error)
    admin.drop = AsyncMock()
    scratch = Scratch("postgresql://host/postgres", admin, sweep_previous=False)

    async def run() -> None:
        with pytest.raises(CheckError):
            async with scratch.session(Mock()):
                pass

    asyncio.run(run())
    admin.drop.assert_not_awaited()
    assert not scratch.leaked
    admin.duplicate.assert_called_once_with(error)


def test_scratch_session_rejects_fewer_than_one_worker() -> None:
    scratch = Scratch("postgresql://u@h/app", Mock(spec=AdminService))
    with pytest.raises(ValueError, match="at least one worker"):
        scratch.session(Mock(), 0)


def test_a_create_error_that_is_not_a_duplicate_leaves_the_drop_pending() -> None:
    import asyncio  # ruff: ignore[import-outside-top-level]
    from unittest.mock import AsyncMock  # ruff: ignore[import-outside-top-level]

    from rowx_core.errors import DatabaseError  # ruff: ignore[import-outside-top-level]

    error = DatabaseError("XX000", "boom")
    admin = Mock(spec=AdminService)
    admin.with_database.return_value = "postgresql://host/scratch"
    admin.create_scratch = AsyncMock(side_effect=error)
    admin.duplicate.return_value = False
    admin.drop = AsyncMock()
    scratch = Scratch("postgresql://host/postgres", admin, sweep_previous=False)
    scratch.pending_drop = True

    async def run() -> None:
        with pytest.raises(CheckError):
            await scratch._create()

    asyncio.run(run())
    admin.duplicate.assert_called_once_with(error)
    assert scratch.pending_drop is True
    assert scratch.leaked


def test_a_backend_scratch_factory_is_used_without_the_admin_service() -> None:
    made = Mock()
    seen: list[tuple[str, bool]] = []

    def factory(dsn: str, /, *, sweep_previous: bool = True) -> Mock:
        seen.append((dsn, sweep_previous))
        return made

    backend = Backend(scratch_factory=factory)
    scratch = scratch_for("fake://db", backend, sweep_previous=False)
    assert isinstance(scratch, BackendScratch)
    assert scratch.inner is made
    assert seen == [("fake://db", False)]


def test_a_backend_without_a_scratch_factory_gets_the_admin_backed_scratch() -> None:
    admin = Mock(spec=AdminService)
    backend = Backend(admin_factory=lambda _dsn: admin)
    scratch = scratch_for("postgresql://u@h/app", backend, sweep_previous=False)
    assert isinstance(scratch, Scratch)
    assert (scratch.server_dsn, scratch.admin, scratch.sweep_previous) == ("postgresql://u@h/app", admin, False)


class FailingScratch:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.swept = 0
        self.opened: list[tuple[MigrationService, int]] = []

    @asynccontextmanager
    async def session(self, migrator: MigrationService, workers: int) -> AsyncGenerator[ScratchBuild]:
        self.opened.append((migrator, workers))
        raise self.error
        yield ScratchBuild(urls=(), seconds=0.0)

    def sweep(self) -> None:
        self.swept += 1


async def _open(scratch: BackendScratch) -> None:
    async with scratch.session(Mock(), 1):
        pass


def test_a_backend_scratch_maps_a_migration_failure_with_its_cause() -> None:
    error = MigrateError("1_init.sql failed")
    error.__cause__ = ValueError("bad sql at fake://u:hunter2secret@h/db")
    wrapped = BackendScratch(FailingScratch(error), "fake://u:hunter2secret@h/db")
    with pytest.raises(CheckError) as caught:
        asyncio.run(_open(wrapped))
    message = str(caught.value)
    assert message.startswith("the migrations failed on the scratch database: 1_init.sql failed: bad sql at ")
    assert "hunter2secret" not in message


def test_a_backend_scratch_maps_an_os_error() -> None:
    wrapped = BackendScratch(
        FailingScratch(OSError("no space on fake://u:hunter2secret@h/db")), "fake://u:hunter2secret@h/db"
    )
    with pytest.raises(CheckError) as caught:
        asyncio.run(_open(wrapped))
    message = str(caught.value)
    assert message.startswith("could not create or migrate the scratch database: no space on ")
    assert "hunter2secret" not in message


def test_a_backend_scratch_delegates_the_sweep() -> None:
    inner = FailingScratch(OSError("x"))
    BackendScratch(inner, "fake://db").sweep()
    assert inner.swept == 1


def test_a_backend_scratch_reports_the_leak_of_what_it_wraps() -> None:
    leaky = Mock(leaked=True)
    assert BackendScratch(leaky, "fake://db").leaked is True
    assert BackendScratch(FailingScratch(OSError("x")), "fake://db").leaked is False

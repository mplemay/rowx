from unittest.mock import Mock

import pytest
from rowx_core.backend import AdminService, Backend
from rowx_core.errors import InterfaceError

from rowx.check.runner import CheckError
from rowx.check.scratch import Scratch, scratch_for


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
    assert scratch_for("fake://db", backend, sweep_previous=False) is made
    assert seen == [("fake://db", False)]


def test_a_backend_without_a_scratch_factory_gets_the_admin_backed_scratch() -> None:
    admin = Mock(spec=AdminService)
    backend = Backend(admin_factory=lambda _dsn: admin)
    scratch = scratch_for("postgresql://u@h/app", backend, sweep_previous=False)
    assert isinstance(scratch, Scratch)
    assert (scratch.server_dsn, scratch.admin, scratch.sweep_previous) == ("postgresql://u@h/app", admin, False)

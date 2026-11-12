from unittest.mock import Mock

import pytest
from rowx_core.backend import AdminService
from rowx_core.errors import InterfaceError

from rowx.check.runner import CheckError
from rowx.check.scratch import Scratch


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

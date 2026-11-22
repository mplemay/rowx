from typing import TYPE_CHECKING

import pytest
from rowx_core.backend import UnsupportedCapabilityError
from rowx_sqlite._backend import load
from rowx_sqlite._scratch import SqliteScratch
from rowx_sqlite._services import SqliteMigrations

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from rowx_core.backend import Backend

DSN = "sqlite:///x.db"


def test_load_registers_the_checker_migration_scratch_and_memory_factories_only() -> None:
    backend = load()
    assert backend.migration_factory is SqliteMigrations
    assert backend.memory_factory is not None
    assert backend.checker_factory is not None
    assert backend.scratch_factory is SqliteScratch
    assert backend.admin_factory is None
    assert backend.testing_factory is None
    assert backend.embedded_factory is None


@pytest.mark.parametrize(
    "capability",
    [
        lambda backend: backend.admin(DSN),
        lambda backend: backend.testing(DSN),
    ],
    ids=["admin", "testing"],
)
def test_the_other_capabilities_are_unsupported(capability: Callable[[Backend], object]) -> None:
    with pytest.raises(UnsupportedCapabilityError):
        capability(load())


def test_migrations_are_supported(tmp_path: Path) -> None:
    service = load().migrations(DSN, tmp_path)
    assert isinstance(service, SqliteMigrations)
    assert service.migrations == ()


@pytest.mark.parametrize(
    ("dsn", "memory"), [("sqlite::memory:", True), (DSN, False), ("sqlite:////abs/path.db", False)]
)
def test_only_the_in_memory_url_is_in_memory(dsn: str, memory: bool) -> None:  # ruff: ignore[boolean-type-hint-positional-argument]
    assert load().in_memory(dsn) is memory


def test_optional_probes_report_nothing() -> None:
    assert load().embedded(DSN) is None

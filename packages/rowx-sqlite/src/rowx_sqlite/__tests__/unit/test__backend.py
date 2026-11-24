from typing import TYPE_CHECKING

import pytest
from rowx_sqlite._admin import SqliteAdmin
from rowx_sqlite._backend import load
from rowx_sqlite._scratch import SqliteScratch
from rowx_sqlite._services import SqliteMigrations
from rowx_sqlite._testing import provisioner

if TYPE_CHECKING:
    from pathlib import Path

DSN = "sqlite:///x.db"


def test_load_registers_every_factory_but_the_embedded_one() -> None:
    backend = load()
    assert backend.migration_factory is SqliteMigrations
    assert backend.memory_factory is not None
    assert backend.checker_factory is not None
    assert backend.scratch_factory is SqliteScratch
    assert backend.admin_factory is SqliteAdmin
    assert backend.testing_factory is provisioner
    assert backend.embedded_factory is None


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

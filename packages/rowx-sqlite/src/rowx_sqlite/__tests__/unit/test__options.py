import dataclasses

import pytest
from rowx_core.errors import InterfaceError
from rowx_sqlite import ConnectOptions


def test_defaults_open_an_existing_file_read_write() -> None:
    options = ConnectOptions(path="x.db")
    assert options.mode == "rw"
    assert not options.memory
    assert not options.read_only
    assert options.connect_timeout is None
    assert options.statement_timeout is None
    assert options.statement_cache_size == 256


def test_options_are_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        ConnectOptions(path="x.db").path = "y.db"  # ty: ignore[invalid-assignment]


def test_from_dsn_reads_a_relative_path() -> None:
    assert ConnectOptions.from_dsn("sqlite:///relative.db") == ConnectOptions(path="relative.db")


def test_from_dsn_reads_an_absolute_path() -> None:
    assert ConnectOptions.from_dsn("sqlite:////abs/path.db").path == "/abs/path.db"


def test_from_dsn_percent_decodes_the_path() -> None:
    assert ConnectOptions.from_dsn("sqlite:///a%20b.db").path == "a b.db"


def test_from_dsn_reads_memory() -> None:
    options = ConnectOptions.from_dsn("sqlite::memory:")
    assert options.memory
    assert options.path is None


def test_from_dsn_overrides_win() -> None:
    options = ConnectOptions.from_dsn("sqlite:///x.db", mode="rwc", statement_timeout=2.0)
    assert (options.path, options.mode, options.statement_timeout) == ("x.db", "rwc", 2.0)


@pytest.mark.parametrize(
    "dsn",
    ["postgresql://localhost/db", "app.db", "sqlite:path", "sqlite://host/x.db", "file:x.db", "sqlite:///x.db?mode=ro"],
)
def test_from_dsn_rejects_other_forms(dsn: str) -> None:
    with pytest.raises(InterfaceError, match="sqlite:///"):
        ConnectOptions.from_dsn(dsn)


def test_with_connect_timeout_returns_a_copy() -> None:
    options = ConnectOptions(path="x.db")
    assert options.with_connect_timeout(1.5).connect_timeout == pytest.approx(1.5)
    assert options.connect_timeout is None

import importlib
import sys

from rowx_sqlite import _core
from rowx_sqlite._core import sqlite_version


def test_core_module_is_named_for_the_package() -> None:
    assert _core.__name__ == "rowx_sqlite._core"
    assert sys.modules["rowx_sqlite._core"] is importlib.import_module("rowx_sqlite._core")


def test_sqlite_version_is_a_submodule_function() -> None:
    assert sqlite_version.__module__ == "rowx_sqlite._core"


def test_sqlite_version_is_the_bundled_release() -> None:
    assert sqlite_version() == "3.51.3"


def test_core_exposes_the_connection_bindings() -> None:
    assert sorted(name for name in dir(_core) if not name.startswith("_")) == [
        "ColumnInfo",
        "Connection",
        "DatabaseInfo",
        "Describe",
        "Migrator",
        "ParamInfo",
        "Pool",
        "RowShape",
        "RowStream",
        "ShapeIssue",
        "TypeInfo",
        "connect",
        "connect_checker",
        "create_database",
        "database_exists",
        "database_ready",
        "describe",
        "drop_database",
        "list_databases",
        "map_columns",
        "parse_dsn",
        "sqlite_version",
        "type_table",
    ]


def test_every_class_names_the_core_module() -> None:
    for cls in (
        _core.Connection,
        _core.DatabaseInfo,
        _core.Migrator,
        _core.Pool,
        _core.RowShape,
        _core.RowStream,
        _core.ShapeIssue,
        _core.TypeInfo,
    ):
        assert cls.__module__ == "rowx_sqlite._core"

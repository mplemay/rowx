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


def test_core_exposes_nothing_else() -> None:
    assert [name for name in dir(_core) if not name.startswith("_")] == ["sqlite_version"]

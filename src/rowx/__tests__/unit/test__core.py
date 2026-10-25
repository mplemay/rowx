import importlib
import sys

from rowx._core.postgres import Connection


def test_postgres_submodule_imports_with_from_import() -> None:
    assert Connection.__module__ == "rowx._core.postgres"


def test_postgres_submodule_imports_with_importlib() -> None:
    module = importlib.import_module("rowx._core.postgres")
    assert module.Connection.__module__ == "rowx._core.postgres"
    assert module.Connection is Connection


def test_postgres_submodule_is_registered_in_sys_modules() -> None:
    assert sys.modules["rowx._core.postgres"] is importlib.import_module("rowx._core.postgres")

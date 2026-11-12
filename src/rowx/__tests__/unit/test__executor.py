import ast
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
from pathlib import Path
from typing import Final

import rowx_core
import rowx_postgres

import rowx
import rowx.postgres as postgres_facade

SCRIPT: Final[str] = (
    "import sys, rowx, rowx_core\n"
    "loaded = [m for m in ('rowx_postgres', 'rowx_postgres._core') if m in sys.modules]\n"
    "assert not loaded, loaded\n"
)
GUARDED_MODULES: Final[frozenset[str]] = frozenset({
    "rowx.testing",
    "rowx.check.database",
    "rowx.check.migrate",
    "rowx.check.scratch",
    "rowx.check.drift",
    "rowx.check.cli",
    "rowx_core._query",
    "rowx_core._executor",
})


def test_import_rowx_does_not_load_a_backend() -> None:
    result = subprocess.run([sys.executable, "-c", SCRIPT], capture_output=True, text=True, check=False)  # ruff: ignore[subprocess-without-shell-equals-true]
    assert result.returncode == 0, result.stderr


def test_backend_names_are_removed_from_rowx() -> None:
    assert not any(hasattr(rowx, name) for name in ("connect", "ping"))
    assert rowx.Connection is rowx_core.Connection
    assert rowx.Executor is rowx_core.Executor


def test_postgres_facade_reexports_the_driver() -> None:
    assert postgres_facade.connect is rowx_postgres.connect
    assert postgres_facade.Pool is rowx_postgres.Pool
    assert postgres_facade.RESET_QUERY is rowx_postgres.RESET_QUERY


def _backend_import(source: str) -> str | None:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "rowx_postgres" or alias.name.startswith("rowx_postgres."):
                    return alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "rowx_postgres" or module.startswith("rowx_postgres."):
                return module
            if any(alias.name == "_core" for alias in node.names):
                return "_core"
    return None


def _core_sources() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for package in (rowx, rowx_core):
        root = Path(package.__file__).resolve().parent
        for path in sorted(root.rglob("*.py")):
            relative = path.relative_to(root)
            if "__tests__" in relative.parts:
                continue
            parts = relative.with_suffix("").parts
            if parts[-1] == "__init__":
                parts = parts[:-1]
            name = ".".join((package.__name__, *parts))
            # This opt-in facade intentionally imports the PostgreSQL package.
            if name == "rowx.postgres":
                continue
            found.append((name, path.read_text(encoding="utf-8")))
    return found


def test_core_modules_do_not_reference_a_backend() -> None:
    sources = _core_sources()
    assert {name for name, _ in sources} >= GUARDED_MODULES
    for name, source in sources:
        assert (reason := _backend_import(source)) is None, f"{name}: {reason}"
    assert _backend_import("import rowx_postgres._backend") == "rowx_postgres._backend"
    assert _backend_import("from rowx_postgres import Connection") == "rowx_postgres"
    assert _backend_import("from rowx_postgres._core import Connection") == "rowx_postgres._core"
    # Static provider metadata can name a module without importing its implementation.
    assert _backend_import('MODULE = "rowx_postgres._backend"\n') is None

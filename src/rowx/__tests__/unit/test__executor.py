import ast
import inspect
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
from io import StringIO
from tokenize import COMMENT, TokenError, generate_tokens
from typing import TYPE_CHECKING, Final

import rowx
import rowx._backend
import rowx._executor
import rowx._query
import rowx._rowplan
import rowx._template
import rowx.check.compare
import rowx.check.discover
import rowx.check.params
import rowx.check.render
import rowx.check.resolve
import rowx.check.runner
import rowx.errors

if TYPE_CHECKING:
    from types import ModuleType

SCRIPT: Final[str] = (
    "import sys, rowx\n"
    "loaded = [m for m in ('rowx.postgres', 'rowx.postgres._core') if m in sys.modules]\n"
    "assert not loaded, loaded\n"
)


def test_import_rowx_does_not_load_a_backend() -> None:
    result = subprocess.run([sys.executable, "-c", SCRIPT], capture_output=True, text=True, check=False)  # ruff: ignore[subprocess-without-shell-equals-true]
    assert result.returncode == 0, result.stderr


def test_backend_names_are_removed_from_rowx() -> None:
    assert not any(hasattr(rowx, name) for name in ("connect", "Connection", "ping"))
    assert hasattr(rowx, "Executor")


CORE_RUNTIME_MODULES: Final[tuple[ModuleType, ...]] = (
    rowx._query,
    rowx._executor,
    rowx._rowplan,
    rowx._template,
    rowx.errors,
)
CHECK_MODULES: Final[tuple[ModuleType, ...]] = (
    rowx.check.compare,
    rowx.check.params,
    rowx.check.resolve,
    rowx.check.runner,
    rowx.check.render,
    rowx.check.discover,
)

FORBIDDEN_BACKEND_SAMPLES: Final[tuple[str, ...]] = (
    "import rowx.postgres",
    "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from rowx.postgres import Connection",
    'value = "rowx.postgres"',
    'value = "rowx.postgres._range"',
    "import rowx._core",
    "from rowx import postgres",
    'name = "range"\nvalue = f"rowx.postgres.{name}"',
    "from rowx.postgres._core import RowShape",
    "import rowx.postgres._core",
    "# see rowx.postgres._types\nx = 1\n",
    "# rowx._core is not a core import\nx = 1\n",
)


def _forbidden_module(name: str) -> bool:
    return (
        name in {"rowx.postgres", "_core"}
        or name.startswith(("rowx.postgres.", "rowx._core"))
        or name.endswith("._core")
    )


def _forbidden_literal(value: str) -> bool:
    return value == "rowx.postgres" or value.startswith(("rowx.postgres.", "rowx._core"))


def _import_name(node: ast.Import) -> str | None:
    for alias in node.names:
        if _forbidden_module(alias.name):
            return alias.name
    return None


def _imported_name(module: str, names: list[str]) -> str | None:
    if module == "rowx" and any(name in {"postgres", "_core"} for name in names):
        return "from rowx import " + ", ".join(names)
    for name in names:
        if name == "_core":
            return name
    return None


def _from_import_name(node: ast.ImportFrom) -> str | None:
    module = node.module or ""
    if module and _forbidden_module(module):
        return module
    return _imported_name(module, [alias.name for alias in node.names])


def _comment_reference(source: str) -> str | None:
    try:
        for token in generate_tokens(StringIO(source).readline):
            if token.type == COMMENT and ("rowx.postgres" in token.string or "rowx._core" in token.string):
                return token.string
    except TokenError, IndentationError, SyntaxError:
        return None
    return None


def _backend_reference(source: str) -> str | None:
    if (comment := _comment_reference(source)) is not None:
        return comment
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            if (found := _import_name(node)) is not None:
                return found
        elif isinstance(node, ast.ImportFrom):
            if (found := _from_import_name(node)) is not None:
                return found
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and _forbidden_literal(node.value):
            return node.value
    return None


def _postgres_import(source: str) -> str | None:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "rowx.postgres" or alias.name.startswith("rowx.postgres."):
                    return alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "rowx.postgres" or module.startswith("rowx.postgres."):
                return module
            if module == "rowx" and any(alias.name == "postgres" for alias in node.names):
                return "rowx.postgres"
    return None


def test_core_runtime_modules_do_not_reference_a_backend() -> None:
    for module in (*CORE_RUNTIME_MODULES, *CHECK_MODULES):
        reason = _backend_reference(inspect.getsource(module))
        assert reason is None, f"{module.__name__}: {reason}"
    for sample in FORBIDDEN_BACKEND_SAMPLES:
        assert _backend_reference(sample) is not None, sample
    assert _backend_reference("from rowx.errors import Error") is None
    assert _postgres_import(inspect.getsource(rowx._backend)) is None
    assert _postgres_import("import rowx.postgres._backend") == "rowx.postgres._backend"
    assert _postgres_import('MODULE = "rowx.postgres._backend"\n') is None

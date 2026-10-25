import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys

import rowx

SCRIPT = (
    "import sys, rowx\n"
    "loaded = [m for m in ('rowx.postgres', 'rowx._core', 'rowx._core.postgres') if m in sys.modules]\n"
    "assert not loaded, loaded\n"
)


def test_import_rowx_does_not_load_a_backend() -> None:
    result = subprocess.run([sys.executable, "-c", SCRIPT], capture_output=True, text=True, check=False)  # ruff: ignore[subprocess-without-shell-equals-true]
    assert result.returncode == 0, result.stderr


def test_backend_names_are_removed_from_rowx() -> None:
    assert not any(hasattr(rowx, name) for name in ("connect", "Connection", "ping"))
    assert hasattr(rowx, "Executor")

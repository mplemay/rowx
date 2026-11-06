import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
from typing import TYPE_CHECKING, Final, get_args

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from rowx._backend import UnsupportedSchemeError, backend_for, module_for
from rowx.check.resolve import HSTORE_LEAF, LEAVES, NETWORK_PAIRS
from rowx.postgres._vocab import LeafName

QUERY: Final[str] = 'from rowx import query\n\nquery[int](t"SELECT 1")\n'
MISSING_BACKEND: Final[str] = """\
import sys
from importlib.abc import MetaPathFinder


class _BlockPostgres(MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "rowx.postgres" or fullname.startswith("rowx.postgres."):
            message = f"no module named {fullname}"
            raise ModuleNotFoundError(message, name=fullname)
        return None


sys.meta_path.insert(0, _BlockPostgres())
assert "rowx.postgres" not in sys.modules
from rowx.check.cli import main

assert "rowx.postgres" not in sys.modules
assert "rowx.check.migrate" not in sys.modules
assert "rowx.check.database" not in sys.modules
assert "rowx.check.drift" not in sys.modules
assert "rowx.check.scratch" not in sys.modules
code = main(["check", "--database-url", "postgres://localhost/db", sys.argv[1]])
assert "rowx.check.migrate" not in sys.modules
assert "rowx.check.database" not in sys.modules
assert "rowx.check.drift" not in sys.modules
assert "rowx.check.scratch" not in sys.modules
raise SystemExit(code)
"""


def test_missing_backend_exits_two(tmp_path: Path) -> None:
    path = tmp_path / "query.py"
    path.write_text(QUERY, encoding="utf-8")
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [sys.executable, "-c", MISSING_BACKEND, str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert "install `rowx[postgres]`" in result.stderr
    assert "internal error" not in result.stderr


@pytest.mark.parametrize(
    "dsn",
    [
        "host=localhost dbname=app",
        "postgres://localhost/app",
        "postgresql://localhost/app",
        "PostgreSQL://localhost/app",
    ],
)
def test_postgres_dsns_select_the_postgres_backend(dsn: str) -> None:
    assert module_for(dsn) == "rowx.postgres._backend"


@pytest.mark.parametrize("dsn", ["sqlite://x", "embedded://x"])
def test_unknown_scheme_does_not_ask_for_the_postgres_extra(dsn: str) -> None:
    with pytest.raises(UnsupportedSchemeError, match="unknown database scheme") as raised:
        module_for(dsn)
    assert "rowx[postgres]" not in str(raised.value)


def test_value_class_leaves_cover_the_vocabulary() -> None:
    backend = backend_for("host=localhost dbname=app")
    found = (
        set(LEAVES.values())
        | set(NETWORK_PAIRS.values())
        | set(backend.value_leaves.values())
        | set(backend.range_leaves.values())
        | {HSTORE_LEAF, "list", "any", "none"}
    )
    assert found == set(get_args(LeafName.__value__))

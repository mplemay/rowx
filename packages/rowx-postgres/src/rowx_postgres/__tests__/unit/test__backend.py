import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
from typing import TYPE_CHECKING, get_args

import pytest
from rowx_postgres._vocab import LeafName

if TYPE_CHECKING:
    from pathlib import Path

from rowx._backend import backend_for
from rowx.check.cli import main
from rowx.check.resolve import HSTORE_LEAF, LEAVES, NETWORK_PAIRS


@pytest.mark.parametrize(
    "argv",
    [
        ["migrate", "info", "--database-url", "postgresql://u:pw@127.0.0.1:1/app"],
        ["database", "create", "--database-url", "postgres://u:pw@127.0.0.1:1/app"],
    ],
)
def test_postgres_url_on_migrate_and_database_is_not_an_unknown_scheme(
    tmp_path: Path, argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # info reads the migration directory before it connects, so the directory has to exist
    if argv[0] == "migrate":
        (tmp_path / "migrations").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    code = main([*argv, "--connect-timeout", "0"])
    err = capsys.readouterr().err
    assert code == 2, err
    assert "unknown database scheme" not in err
    assert "could not reach the database" in err
    assert "rowx[postgres]" not in err


def test_value_class_leaves_cover_the_vocabulary() -> None:
    backend = backend_for("host=localhost dbname=app").checker()
    assert backend.param_classes is backend.value_classes
    found = (
        set(LEAVES.values())
        | set(NETWORK_PAIRS.values())
        | set(backend.value_leaves.values())
        | set(backend.range_leaves.values())
        | {HSTORE_LEAF, "list", "any", "none"}
    )
    assert found == set(get_args(LeafName.__value__))


@pytest.mark.parametrize(
    "statement", ["import rowx", "import rowx_postgres", "import rowx.postgres", "import rowx.check.cli"]
)
def test_importing_never_imports_the_embedded_companion(statement: str) -> None:
    code = f"import sys\n{statement}\nassert 'rowx_embedded' not in sys.modules\n"
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

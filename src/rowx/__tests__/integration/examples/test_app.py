import os
from pathlib import Path
from shutil import copytree
from subprocess import run  # ruff: ignore[suspicious-subprocess-import]
from sys import executable
from typing import Final

import pytest

from rowx.check.cli import main

pytestmark = [pytest.mark.integration, pytest.mark.db]

APP: Final[Path] = Path(__file__).resolve().parents[5] / "examples" / "app"


def _run_main(env: dict[str, str]) -> tuple[int, str, str]:
    result = run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [executable, str(APP / "main.py")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.returncode, result.stdout, result.stderr


def test_main_runs_and_is_repeatable(app_schema: str) -> None:
    for _ in range(2):
        code, out, err = _run_main({**os.environ, "DATABASE_URL": app_schema})
        assert code == 0, err
        assert "Octavia Butler: 2 books" in out
        assert "inside the transaction: 3 books" in out
        assert "after the rollback: 2 books" in out


def test_main_requires_a_database_url() -> None:
    code, _, err = _run_main({key: value for key, value in os.environ.items() if key != "DATABASE_URL"})
    assert code != 0
    assert "DATABASE_URL is not set" in err


def test_check_passes(app_schema: str, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["check", str(APP), "--database-url", app_schema])
    captured = capsys.readouterr()
    assert (code, captured.out, captured.err) == (0, "", "")


def test_check_catches_a_broken_annotation(app_schema: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    broken = tmp_path / "app"
    copytree(APP, broken)
    source = (broken / "main.py").read_text()
    # bio is nullable, so dropping `| None` must be reported
    old = "type AuthorRow = tuple[int, str, str | None]"
    assert old in source
    (broken / "main.py").write_text(source.replace(old, "type AuthorRow = tuple[int, str, str]"))
    code = main(["check", str(broken), "--database-url", app_schema])
    out = capsys.readouterr().out
    assert code == 1
    assert "error[nullability-mismatch]" in out

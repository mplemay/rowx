import os
import shutil
from pathlib import Path
from subprocess import run  # ruff: ignore[suspicious-subprocess-import]
from sys import executable
from typing import Final

import pytest

from rowx.check.cli import main

pytest_plugins = ["pytester"]

# these need the companion, not a server, so they run on every lane that has it
pytestmark = [pytest.mark.embedded]

EXAMPLE: Final[Path] = (
    next(parent for parent in Path(__file__).resolve().parents if (parent / "Cargo.lock").is_file())
    / "examples"
    / "embedded"
)
IGNORED: Final[tuple[str, ...]] = ("__pycache__", ".pytest_cache")


def _run_app(env: dict[str, str]) -> tuple[int, str, str]:
    result = run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [executable, str(EXAMPLE / "notes.py")], check=False, capture_output=True, text=True, env=env
    )
    return result.returncode, result.stdout, result.stderr


def _env(url: str | None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key != "DATABASE_URL"}
    return env if url is None else {**env, "DATABASE_URL": url}


def test_app_runs_in_memory_with_no_database_url() -> None:
    code, out, err = _run_app(_env(None))
    assert code == 0, err
    assert out.count("applied ") == 2
    assert "2: Read the PEP (PEP 750)" in out
    assert "errands: Buy milk, Read the PEP" in out
    assert "errand: 2\nreading: 1\n" in out


def test_app_keeps_its_rows_in_a_directory(tmp_path: Path) -> None:
    env = _env(f"embedded://{tmp_path / 'notes-db'}")
    code, out, err = _run_app(env)
    assert code == 0, err
    assert out.count("applied ") == 2
    code, out, err = _run_app(env)
    assert code == 0, err
    assert "applied " not in out
    # the first run's two notes are still there (identity values can skip ahead after a restart, so ids are not asserted)
    assert out.count("Read the PEP (PEP 750)") == 2


@pytest.mark.parametrize("jobs", ["1", "4"])
def test_check_passes_against_an_in_memory_database(
    jobs: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(EXAMPLE)
    monkeypatch.setenv("DATABASE_URL", "embedded://")
    assert main(["check", "-j", jobs]) == 0
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")


def test_check_reports_a_query_that_does_not_match_the_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    shutil.copytree(EXAMPLE, tmp_path / "copy", ignore=shutil.ignore_patterns(*IGNORED))
    source = tmp_path / "copy" / "notes.py"
    source.write_text(
        source.read_text().replace("SELECT id, title, body FROM notes", "SELECT id, title, bogus FROM notes")
    )
    monkeypatch.chdir(tmp_path / "copy")
    monkeypatch.setenv("DATABASE_URL", "embedded://")
    assert main(["check"]) == 1
    assert 'column "bogus" does not exist' in capsys.readouterr().out


def test_the_plugin_tests_pass(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "embedded://")
    shutil.copytree(EXAMPLE, pytester.path, dirs_exist_ok=True, ignore=shutil.ignore_patterns(*IGNORED))
    # as CI runs it
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-o", "rowx_require_database=true")
    result.assert_outcomes(passed=4)

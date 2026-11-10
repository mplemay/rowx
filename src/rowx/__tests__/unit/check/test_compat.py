import subprocess  # ruff: ignore[suspicious-subprocess-import]
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from rowx.check import compat
from rowx.check.cli import main

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def git(root: Path, *args: str) -> str:
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        ["git", *args],  # ruff: ignore[start-process-with-partial-path]
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def revision(root: Path, config: str) -> str:
    git(root, "init", "-q")
    (root / "pyproject.toml").write_text(config, encoding="utf-8")
    (root / "migrations").mkdir()
    (root / "migrations" / "1_init.sql").write_text("SELECT 1", encoding="utf-8")
    git(root, "add", ".")
    git(root, "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "fixture")
    return git(root, "rev-parse", "HEAD")


def test_invalid_server_url_is_redacted_and_identifies_schema_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = revision(tmp_path, "[tool.rowx.migrate]\n")
    monkeypatch.chdir(tmp_path)
    status = main([
        "check-compat",
        "--against",
        "HEAD",
        "--head",
        "HEAD",
        "--database-url",
        "postgresql://user:fake-marker@[bad",
    ])
    captured = capsys.readouterr()
    assert status == 2
    assert sha in captured.err
    assert "building schema" in captured.err
    assert "fake-marker" not in captured.err + captured.out
    assert "Traceback" not in captured.err
    assert "compatible checked subset" not in captured.out


def test_malformed_historical_tool_table_is_an_incomplete_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = revision(tmp_path, 'tool = "malformed"\n')
    monkeypatch.chdir(tmp_path)
    status = main(["check-compat", "--against", "HEAD", "--head", "HEAD", "--database-url", "postgresql://host/db"])
    captured = capsys.readouterr()
    assert status == 2
    assert sha in captured.err
    assert "loading revision" in captured.err
    assert "[tool] must be a table" in captured.err
    assert "Traceback" not in captured.err
    assert "compatible checked subset" not in captured.out


def test_snapshot_cleanup_error_prevents_compatible_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class BrokenDirectory:
        def __enter__(self) -> str:
            return str(tmp_path)

        def __exit__(self, *_args: object) -> None:
            msg = "snapshot cleanup failed"
            raise OSError(msg)

    monkeypatch.setattr(compat, "TemporaryDirectory", lambda **_kwargs: BrokenDirectory())
    monkeypatch.setattr(compat, "_snapshot", lambda *_args: object())
    monkeypatch.setattr(compat, "_run", AsyncMock(return_value=0))
    status = main(["check-compat", "--against", "HEAD", "--head", "HEAD", "--database-url", "postgresql://host/db"])
    captured = capsys.readouterr()
    assert status == 2
    assert "snapshot cleanup failed" in captured.err
    assert "compatible checked subset" not in captured.out

import subprocess  # ruff: ignore[suspicious-subprocess-import]
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from rowx.check import compat
from rowx.check.cli import main

if TYPE_CHECKING:
    from pathlib import Path


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


def test_materialize_large_blob_exactly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    revision(repo, "[tool.rowx.migrate]\n")
    content = bytes(range(256)) * 1025
    (repo / "large.bin").write_bytes(content)
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "large")
    destination = tmp_path / "snapshot"
    destination.mkdir()
    monkeypatch.chdir(repo)
    compat._materialize(git(repo, "rev-parse", "HEAD"), destination)
    assert (destination / "large.bin").read_bytes() == content


@pytest.mark.parametrize("names", [("source/Q.py", "source/q.py"), ("Directory/a.py", "directory/b.py")])
def test_materialize_rejects_native_case_collisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, names: tuple[str, str]
) -> None:

    repo = tmp_path / "repo"
    repo.mkdir()
    revision(repo, "[tool.rowx.migrate]\n")
    oid = git(repo, "hash-object", "-w", "pyproject.toml")
    for name in names:
        git(repo, "update-index", "--add", "--cacheinfo", f"100644,{oid},{name}")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "collisions")
    destination = tmp_path / "snapshot"
    destination.mkdir()
    probe = tmp_path / "caseprobe"
    probe.mkdir()
    (probe / "Q").touch()
    if not (probe / "q").exists():
        pytest.skip("filesystem is case-sensitive")
    monkeypatch.chdir(repo)
    with pytest.raises(compat.CheckError, match="filesystem collision"):
        compat._materialize(git(repo, "rev-parse", "HEAD"), destination)

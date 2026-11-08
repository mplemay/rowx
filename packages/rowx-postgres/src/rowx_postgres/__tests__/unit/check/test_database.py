from typing import TYPE_CHECKING

from rowx.check.cli import main

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_malformed_database_url_exits_two_without_exposing_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["database", "create", "--database-url", "postgresql://u:hunter2secret@[broken"]) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert "could not be parsed" in captured.err
    assert "hunter2secret" not in captured.err
    assert "Traceback" not in captured.err


def test_unreachable_database_exits_two_without_the_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    url = "postgresql://role:hunter2pw@127.0.0.1:1/app"
    assert main(["database", "create", "--database-url", url, "--connect-timeout", "0"]) == 2
    captured = capsys.readouterr()
    assert "hunter2pw" not in captured.out + captured.err
    assert url not in captured.out + captured.err


def test_url_without_a_database_name_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["database", "drop", "-y", "--database-url", "postgresql://role:pw@127.0.0.1:1/"]) == 2
    assert "names no database" in capsys.readouterr().err

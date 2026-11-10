from typing import TYPE_CHECKING

from rowx.check.cli import main

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_coverage_and_policies_without_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    path = tmp_path / "sites.py"
    path.write_text("from rowx import query_unchecked\nquery_unchecked(t'SELECT 1')\n", encoding="utf-8")
    assert main(["check", str(path)]) == 0
    assert not capsys.readouterr().out
    assert main(["check", str(path), "--coverage", "--require-checked", "--disallow-unchecked"]) == 1
    out = capsys.readouterr().out
    assert "coverage-require-checked" in out
    assert "coverage-unchecked" in out
    assert "sites.py:2:1-2:29: unchecked" in out
    path.write_text("x = 1\n", encoding="utf-8")
    assert main(["check", str(path), "--coverage", "--require-checked"]) == 1
    assert "no recognized query sites" in capsys.readouterr().out


def test_coverage_json_keeps_diagnostics_array(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    path = tmp_path / "empty.py"
    path.write_text("x = 1\n", encoding="utf-8")
    assert main(["check", str(path), "--coverage", "--output-format", "json"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "[]\n"
    assert "no recognized query sites" in captured.err


def test_coverage_includes_rejected_query_without_changing_statistics(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    path = tmp_path / "rejected.py"
    path.write_text("from rowx import query\nquery[int](sql)\n", encoding="utf-8")
    assert main(["check", str(path), "--coverage", "--statistics"]) == 1
    out = capsys.readouterr().out
    assert "error[dynamic-query]" in out
    assert "call sites found: 0" in out
    assert "recognized query sites: 1" in out
    assert "rejected.py:2:1-2:16: not described" in out

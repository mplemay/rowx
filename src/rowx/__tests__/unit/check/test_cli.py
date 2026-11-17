from typing import TYPE_CHECKING

import pytest

from rowx.check.cli import main

if TYPE_CHECKING:
    from pathlib import Path


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


def test_rules_warning_precedence_json_and_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.rowx.rules]\nall = "ignore"\ndynamic-query = "warn"\n', encoding="utf-8"
    )
    (tmp_path / "site.py").write_text("from rowx import query\nquery[int](sql)\n", encoding="utf-8")
    assert main(["check", "--output-format", "json", "--statistics"]) == 1
    captured = capsys.readouterr()
    assert '"severity": "warning"' in captured.out
    assert "diagnostics: 1" in captured.err
    assert main(["check", "--exit-zero-on-warning"]) == 0
    assert "warning[dynamic-query]" in capsys.readouterr().out
    assert main(["check", "--error", "all", "--ignore", "dynamic-query"]) == 0
    capsys.readouterr()
    assert main(["check", "--ignore", "all", "--error", "dynamic-query"]) == 1
    assert "error[dynamic-query]" in capsys.readouterr().out


def test_ignore_and_coverage_policy_are_reporting_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "site.py").write_text(
        'from rowx import query_unchecked\nquery_unchecked(t"SELECT 1") # rowx: ignore[coverage-unchecked]\n',
        encoding="utf-8",
    )
    assert main(["check", "--coverage", "--disallow-unchecked"]) == 0
    out = capsys.readouterr().out
    assert "recognized query sites: 1" in out
    assert "coverage-unchecked" not in out


def test_syntax_errors_require_complete_tokenization(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "site.py"
    path.write_text("# rowx: ignore[syntax-error]\nx = :\n", encoding="utf-8")
    assert main(["check"]) == 0
    capsys.readouterr()
    path.write_text("# rowx: ignore[syntax-error]\nx = (\n", encoding="utf-8")
    assert main(["check"]) == 1
    assert "syntax-error" in capsys.readouterr().out


def test_rule_config_validation_and_warning_setting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "pyproject.toml"
    for value in ('rules = "bad"', '[tool.rowx.rules]\ntypo = "warn"', '[tool.rowx.rules]\nall = "bad"'):
        path.write_text(("[tool.rowx]\n" if not value.startswith("[") else "") + value, encoding="utf-8")
        assert main(["check"]) == 2
        assert "error:" in capsys.readouterr().err
    path.write_text("[tool.rowx.terminal]\nerror-on-warning = false\n", encoding="utf-8")
    (tmp_path / "site.py").write_text("x = 1 # rowx: ignore[sql-error]\n", encoding="utf-8")
    assert main(["check"]) == 0
    assert "warning[unused-ignore-comment]" in capsys.readouterr().out


def test_invalid_ignore_file_is_a_configuration_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ignore").write_bytes(b"\xff")
    (tmp_path / "site.py").write_text("x = 1\n", encoding="utf-8")
    assert main(["check"]) == 2
    assert ".ignore" in capsys.readouterr().err


@pytest.mark.parametrize("ignore_file", [".gitignore", ".ignore"])
@pytest.mark.parametrize("pattern", ["!", "abc\\"])
def test_malformed_ignore_pattern_is_a_configuration_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, ignore_file: str, pattern: str
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ignore_file).write_text(pattern + "\n", encoding="utf-8")
    (tmp_path / "site.py").write_text("x = 1\n", encoding="utf-8")
    assert main(["check"]) == 2
    captured = capsys.readouterr()
    assert ignore_file in captured.err
    assert "Traceback" not in captured.err


def test_checker_never_executes_source_with_ignore(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    sentinel = tmp_path / "executed"
    (tmp_path / "site.py").write_text(
        f'from pathlib import Path\nPath({str(sentinel)!r}).write_text("executed")\n'
        "from rowx import query\nquery[int](sql) # rowx: ignore[dynamic-query]\n",
        encoding="utf-8",
    )
    assert main(["check"]) == 0
    assert not sentinel.exists()
    assert not capsys.readouterr().out

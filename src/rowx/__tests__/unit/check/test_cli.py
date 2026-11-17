import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Final, cast

import pytest
from rowx_core.backend import Backend, CheckerBackend, ScratchBuild
from rowx_core.errors import DatabaseError
from rowx_postgres._compiler import COMPILER

from rowx._backend import LOADED, SCHEMES
from rowx.check.cli import _coverage, _inspect, main
from rowx.check.model import Span
from rowx.check.runner import (
    InspectedColumn,
    InspectedParameter,
    InspectedSite,
    ParameterCoverage,
    RunResult,
    SiteCoverage,
    Statistics,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Sequence

    from rowx_core.backend import CheckerSession, MigrationService, ShapeIssue

QUERY: Final[str] = 'from rowx import query\n\nquery[tuple[int]](t"SELECT 1")\n'


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


def unused_mapping(*_args: object) -> tuple[Sequence[int | None], Sequence[ShapeIssue]]:
    message = "the checker is never reached"
    raise AssertionError(message)


def unused_connect(_dsn: str, _timeout: float) -> Awaitable[CheckerSession]:
    message = "the checker is never reached"
    raise AssertionError(message)


class FakeMigrator:
    migrations: tuple[()] = ()


class FakeScratch:
    def __init__(self, dsn: str, *, sweep_previous: bool, events: list[str]) -> None:
        self.dsn = dsn
        self.sweep_previous = sweep_previous
        self.events = events

    @asynccontextmanager
    async def session(self, _migrator: MigrationService, workers: int) -> AsyncGenerator[ScratchBuild]:
        self.events.append(f"scratch.start {workers}")
        try:
            yield ScratchBuild(urls=("fakemem:///scratch",), seconds=0.0)
        finally:
            self.events.append("scratch.close")

    def sweep(self) -> None:
        self.events.append("sweep")


def install_memory_world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, migrate: bool = True) -> list[str]:
    events: list[str] = []

    def scratch(dsn: str, /, *, sweep_previous: bool = True) -> FakeScratch:
        events.append(f"factory {dsn} {sweep_previous}")
        return FakeScratch(dsn, sweep_previous=sweep_previous, events=events)

    def checker() -> CheckerBackend:
        return CheckerBackend(
            connect_checker=unused_connect,
            compiler=COMPILER,
            map_columns=unused_mapping,
            types={},
            param_classes={},
            value_leaves={},
            range_leaves={},
            range_classes=frozenset(),
            value_classes={},
        )

    backend = Backend(
        checker_factory=checker,
        scratch_factory=scratch,  # ty: ignore[invalid-argument-type]
        memory_factory=lambda url: url == "fakemem://:memory:",
        migration_factory=lambda *_args, **_options: cast("MigrationService", FakeMigrator()),
    )
    monkeypatch.setitem(sys.modules, "fake_memory_backend", SimpleNamespace(load=lambda: backend))
    monkeypatch.setitem(SCHEMES, "fakemem", ("fake_memory_backend", "fake"))
    monkeypatch.delitem(LOADED, "fake_memory_backend", raising=False)

    async def describe_all(urls: Sequence[str], statements: list[str], *_args: object) -> dict[str, DatabaseError]:  # ruff: ignore[unused-async]
        events.append(f"describe {','.join(urls)}")
        return {sql: DatabaseError("42P01", "relation does not exist") for sql in statements}

    async def check_drift(_config: object, url: str, _backend: object = None) -> int:  # ruff: ignore[unused-async]
        events.append(f"gate {url}")
        return 1

    monkeypatch.setattr("rowx.check.runner.describe_all", describe_all)
    monkeypatch.setattr("rowx.check.drift.check_drift", check_drift)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    (tmp_path / "pyproject.toml").write_text("[tool.rowx.migrate]\n" if migrate else "", encoding="utf-8")
    (tmp_path / "migrations").mkdir()
    (tmp_path / "q.py").write_text(QUERY, encoding="utf-8")
    return events


def test_an_in_memory_url_implies_scratch_skips_the_gate_and_goes_through_the_scratch_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = install_memory_world(monkeypatch, tmp_path)
    assert main(["check", "q.py", "--database-url", "fakemem://:memory:"]) == 1
    assert events == [
        "factory fakemem://:memory: True",
        "scratch.start 1",
        "describe fakemem:///scratch",
        "scratch.close",
        "sweep",
    ]


def test_an_in_memory_url_without_migrate_config_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    events = install_memory_world(monkeypatch, tmp_path, migrate=False)
    assert main(["check", "q.py", "--database-url", "fakemem://:memory:"]) == 2
    assert capsys.readouterr().err == (
        "error: an in-memory database URL implies `--scratch`, which needs a `[tool.rowx.migrate]` table "
        "in pyproject.toml\n"
    )
    assert events == []


def test_a_file_url_of_the_same_backend_keeps_the_gate_and_builds_no_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = install_memory_world(monkeypatch, tmp_path)
    assert main(["check", "q.py", "--database-url", "fakemem:///var/app.db"]) == 1
    assert events[0] == "gate fakemem:///var/app.db"
    assert not any(event.startswith(("factory", "scratch")) for event in events)


def test_an_explicit_scratch_goes_through_the_scratch_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events = install_memory_world(monkeypatch, tmp_path)
    assert main(["check", "q.py", "--scratch", "--database-url", "fakemem:///var/app.db"]) == 1
    assert events[0] == "factory fakemem:///var/app.db True"
    assert not any(event.startswith("gate") for event in events)


def coverage_result(*parameters: ParameterCoverage) -> RunResult:
    span = Span(line=3, col=1, end_line=3, end_col=9)
    site = SiteCoverage(
        path=Path("q.py"),
        span=span,
        kind="query",
        described=True,
        row_comparison="compared",
        parameters_aligned=True,
        parameters=parameters,
    )
    return RunResult(reports=[], sources={}, statistics=Statistics(), coverage=[site])


def test_coverage_names_each_parameter_as_the_backend_does_and_says_when_it_is_untyped() -> None:
    span = Span(line=3, col=5, end_line=3, end_col=6)
    typed = ParameterCoverage(
        span=span, sql_type="integer", sql_type_key="int4", sql_element_key=None, python_type=None, mismatch=None
    )
    untyped = ParameterCoverage(
        span=span,
        sql_type="",
        sql_type_key=None,
        sql_element_key=None,
        python_type=None,
        mismatch=None,
        label="?2",
        untyped=True,
    )
    out = _coverage(coverage_result(typed, untyped), Path.cwd())
    assert "    parameter $1: SQL integer, Python unknown; not compared\n" in out
    assert "    parameter ?2: SQL , Python unknown; untyped, not compared\n" in out


def test_inspect_names_each_parameter_and_column_as_the_backend_does_and_says_when_untyped() -> None:
    site = InspectedSite(
        path="q.py",
        span=Span(line=3, col=1, end_line=3, end_col=9),
        columns=(
            InspectedColumn(name="v", sql_type="", python_type=None, nullable=True, reason="r", untyped=True),
            InspectedColumn(name="w", sql_type="blob", python_type=None, nullable=True, reason="r"),
        ),
        parameters=(
            InspectedParameter(sql_type="", python_type=None, mismatch=None, label="?1", untyped=True),
            InspectedParameter(sql_type="integer", python_type=None, mismatch=None),
        ),
    )
    out = _inspect(RunResult(reports=[], sources={}, statistics=Statistics(), inspection=[site]))
    assert '  column 1: "v", SQL "", Python untyped, nullable, "r"\n' in out
    assert '  column 2: "w", SQL "blob", Python unknown, nullable, "r"\n' in out
    assert '  parameter ?1: SQL "", Python unknown, untyped, not compared\n' in out
    assert '  parameter $2: SQL "integer", Python unknown, not compared\n' in out

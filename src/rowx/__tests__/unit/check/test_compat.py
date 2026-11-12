import subprocess  # ruff: ignore[suspicious-subprocess-import]
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from types import SimpleNamespace
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


def test_snapshot_keeps_tracked_ignored_query_in_compatibility_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from rowx.check.discover import discover_file  # ruff: ignore[import-outside-top-level]
    from rowx.check.model import Diagnostic  # ruff: ignore[import-outside-top-level]
    from rowx.check.render import Report  # ruff: ignore[import-outside-top-level]
    from rowx.check.runner import RunResult, SiteCoverage, Statistics  # ruff: ignore[import-outside-top-level]

    repo = tmp_path / "repo"
    repo.mkdir()
    revision(repo, "[tool.rowx.migrate]\n")
    (repo / "good.py").write_text('from rowx import query\nquery[tuple[int]](t"select 1")\n', encoding="utf-8")
    (repo / "bad.py").write_text('from rowx import query\nquery[tuple[int]](t"select 2")\n', encoding="utf-8")
    git(repo, "add", "good.py", "bad.py")
    (repo / ".gitignore").write_text("bad.py\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "queries")
    monkeypatch.chdir(repo)
    seen: list[list[str]] = []

    async def check(_config: object, files: list[Path], *_args: object, **_kwargs: object) -> RunResult:  # ruff: ignore[unused-async]
        seen.append([file.name for file in files])
        calls = [call for file in files for call in discover_file(file).calls]
        coverage = [
            SiteCoverage(
                path=call.path,
                span=call.span,
                kind=call.kind,
                described=True,
                row_comparison="compared",
                parameters_aligned=True,
            )
            for call in calls
        ]
        reports = []
        if len(seen) > 2:
            reports = [
                Report(diagnostic=Diagnostic(rule="sql-error", message="incompatible", path=call.path, span=call.span))
                for call in calls
                if call.path.name == "bad.py"
            ]
        return RunResult(reports=reports, sources={}, statistics=Statistics(), coverage=coverage)

    @asynccontextmanager
    async def session(_migrator: object, _workers: int):
        yield SimpleNamespace(urls=("postgresql://example",), seconds=0.0)

    class FakeScratch:
        leaked = False

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        @staticmethod
        def session(migrator: object, workers: int) -> AbstractAsyncContextManager[SimpleNamespace]:
            return session(migrator, workers)

        def sweep(self) -> None:
            pass

    monkeypatch.setattr(compat, "Scratch", FakeScratch)
    monkeypatch.setattr(compat, "backend_for", lambda _dsn: SimpleNamespace(admin=lambda _dsn: None))
    monkeypatch.setattr(compat, "load_migrator", lambda *_args: None)
    monkeypatch.setattr(compat, "run_check", check)
    status = main(["check-compat", "--against", "HEAD", "--head", "HEAD", "--database-url", "postgresql://host/db"])
    assert seen == [["bad.py", "good.py"]] * 4
    assert status == 1
    assert "incompatible" in capsys.readouterr().out


def test_embedded_directory_url_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    revision(tmp_path, "[tool.rowx.migrate]\n")
    monkeypatch.chdir(tmp_path)
    status = main(["check-compat", "--against", "HEAD", "--head", "HEAD", "--database-url", "embedded:///var/db"])
    captured = capsys.readouterr()
    assert status == 2
    assert "use `embedded://` and not an embedded directory URL" in captured.err
    assert "Traceback" not in captured.err
    assert not captured.out


def test_embedded_url_builds_each_schema_with_one_instance_per_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from rowx_core.backend import ScratchBuild  # ruff: ignore[import-outside-top-level]

    from rowx.check.discover import discover_file  # ruff: ignore[import-outside-top-level]
    from rowx.check.runner import Hooks, RunResult, SiteCoverage, Statistics  # ruff: ignore[import-outside-top-level]

    revision(tmp_path, "[tool.rowx.migrate]\n")
    (tmp_path / "good.py").write_text('from rowx import query\nquery[tuple[int]](t"select 1")\n', encoding="utf-8")
    git(tmp_path, "add", "good.py")
    git(tmp_path, "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "query")
    monkeypatch.chdir(tmp_path)
    requested: list[int] = []

    @asynccontextmanager
    async def session(_service: object, _migrator: object, workers: int):
        requested.append(workers)
        tag = len(requested)
        yield ScratchBuild(
            urls=tuple(
                f"postgresql://postgres@/postgres?host=/run/rx{tag}-{n}&sslmode=disable" for n in range(workers)
            ),
            seconds=0.0,
        )

    seen: list[tuple[str | None, tuple[str, ...]]] = []

    async def check(
        _config: object, files: list[Path], dsn: str | None, _jobs: int, hooks: Hooks, *, boundary: Path
    ) -> RunResult:
        assert boundary
        assert hooks.scratch is not None
        async with hooks.scratch("embedded://", 2) as built:
            seen.append((dsn, built.urls))
        coverage = [
            SiteCoverage(
                path=call.path,
                span=call.span,
                kind=call.kind,
                described=True,
                row_comparison="compared",
                parameters_aligned=True,
            )
            for file in files
            for call in discover_file(file).calls
        ]
        return RunResult(reports=[], sources={}, statistics=Statistics(), coverage=coverage)

    monkeypatch.setattr(compat, "scratch_session", session)
    monkeypatch.setattr(compat, "load_migrator", lambda *_args: None)
    monkeypatch.setattr(compat, "run_check", check)
    status = main([
        "check-compat",
        "--against",
        "HEAD",
        "--head",
        "HEAD",
        "--database-url",
        "embedded://",
        "-j",
        "2",
    ])
    captured = capsys.readouterr()
    assert status == 0
    assert requested == [2, 2]
    assert [dsn for dsn, _ in seen] == [urls[0] for _, urls in seen]
    assert [len(urls) for _, urls in seen] == [2, 2, 2, 2]
    assert len({urls for _, urls in seen}) == 2
    assert "compatible checked subset" in captured.out
    assert "disposable PostgreSQL server" not in captured.err

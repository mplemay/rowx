import sys
from pathlib import Path
from typing import Final

import pytest

from rowx.__tests__.integration.check.harness import CASES_DIR
from rowx.check.cli import main

pytestmark = [pytest.mark.integration, pytest.mark.db]

APP: Final[Path] = Path(__file__).resolve().parents[5] / "examples" / "app"


def _run(capsys: pytest.CaptureFixture[str], target: Path, dsn: str, jobs: int, fmt: str) -> tuple[int, str, str]:
    code = main(["check", str(target), "--database-url", dsn, "-j", str(jobs), "--output-format", fmt])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _user_modules(root: Path) -> list[str]:
    resolved = root.resolve()
    return sorted(
        name
        for name, module in sys.modules.items()
        if (file := getattr(module, "__file__", None)) is not None and Path(file).resolve().is_relative_to(resolved)
    )


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_corpus_output_is_identical_across_job_counts(
    capsys: pytest.CaptureFixture[str],
    schema: str,
    fmt: str,
) -> None:
    serial = _run(capsys, CASES_DIR, schema, 1, fmt)
    parallel = _run(capsys, CASES_DIR, schema, 8, fmt)
    # the corpus is full of diagnostics, so equal output cannot be equal emptiness
    assert serial[0] == 1
    assert serial[1]
    assert serial == parallel


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_app_output_is_identical_across_job_counts(
    capsys: pytest.CaptureFixture[str],
    app_schema: str,
    fmt: str,
) -> None:
    serial = _run(capsys, APP, app_schema, 1, fmt)
    parallel = _run(capsys, APP, app_schema, 8, fmt)
    assert serial == parallel
    assert serial[0] == 0


@pytest.mark.parametrize(("target", "use_app"), [(CASES_DIR, False), (APP, True)], ids=["corpus", "app"])
def test_check_imports_no_user_module(
    capsys: pytest.CaptureFixture[str],
    schema: str,
    app_schema: str,
    target: Path,
    use_app: bool,  # ruff: ignore[boolean-type-hint-positional-argument]
) -> None:
    dsn = app_schema if use_app else schema
    # a first run settles rowx's own lazy imports, so that the snapshot only sees user code
    _run(capsys, target, dsn, 1, "text")
    before = set(sys.modules)
    _run(capsys, target, dsn, 8, "json")
    assert set(sys.modules) - before == set()
    assert _user_modules(target) == []

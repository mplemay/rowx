import sys
from collections.abc import Callable
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from shutil import copy2
from tomllib import loads
from typing import Final, cast

import pytest

REPO: Final[Path] = Path(__file__).resolve().parents[4]
SCRIPT: Final[Path] = REPO / "scripts" / "validate-release-versions.py"
CHECKER_MODULE: Final[str] = "rowx_test_validate_release_versions"
MISMATCH_VERSION: Final[str] = "9.9.9"
VERSION_FILES: Final[tuple[str, ...]] = (
    "pyproject.toml",
    "Cargo.toml",
    "Cargo.lock",
    "uv.lock",
    "packages/rowx-core/pyproject.toml",
    "packages/rowx-postgres/pyproject.toml",
)

type Main = Callable[[], int]


def _load_main() -> Main:
    spec = spec_from_file_location(CHECKER_MODULE, SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[CHECKER_MODULE] = module
    spec.loader.exec_module(module)
    return cast("Main", module.__dict__["main"])


MAIN: Final[Main] = _load_main()


def _copy_version_files(destination: Path) -> None:
    for relative in VERSION_FILES:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        copy2(REPO / relative, target)


def _release_version(root: Path) -> str:
    version = loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert isinstance(version, str)
    return version


def _run(
    monkeypatch: pytest.MonkeyPatch,
    event_name: str,
    *,
    root: Path | None = None,
    output: Path | None = None,
) -> int:
    monkeypatch.delenv("TAG_NAME", raising=False)
    if output is None:
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    else:
        monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("EVENT_NAME", event_name)
    argv = [str(SCRIPT)]
    if root is not None:
        argv.extend(["--root", str(root)])
    monkeypatch.setattr(sys, "argv", argv)
    return MAIN()


def test_repository_versions_agree_without_a_tag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert _run(monkeypatch, "workflow_dispatch") == 0
    output = tmp_path / "github-output.txt"
    assert _run(monkeypatch, "pull_request", output=output) == 0
    assert output.read_text(encoding="utf-8") == f"version={_release_version(REPO)}\n"


def test_push_without_tag_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit, match="TAG_NAME is not set"):
        _run(monkeypatch, "push")


def test_mismatched_dev_pin_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _copy_version_files(tmp_path)
    project = tmp_path / "pyproject.toml"
    version = _release_version(tmp_path)
    text = project.read_text(encoding="utf-8")
    needle = f'    "rowx-postgres=={version}",\n'
    assert text.count(needle) == 1
    project.write_text(text.replace(needle, f'    "rowx-postgres=={MISMATCH_VERSION}",\n', 1), encoding="utf-8")
    with pytest.raises(SystemExit, match="dev dependency group"):
        _run(monkeypatch, "pull_request", root=tmp_path)


def test_mismatched_lock_version_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _copy_version_files(tmp_path)
    lock = tmp_path / "uv.lock"
    version = _release_version(tmp_path)
    text = lock.read_text(encoding="utf-8")
    needle = f'name = "rowx-core"\nversion = "{version}"'
    assert needle in text
    lock.write_text(
        text.replace(needle, f'name = "rowx-core"\nversion = "{MISMATCH_VERSION}"', 1),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="Release version mismatch"):
        _run(monkeypatch, "pull_request", root=tmp_path)

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from shutil import copy2
from typing import Final, Protocol, cast

import pytest

REPO: Final[Path] = Path(__file__).resolve().parents[4]
SCRIPT: Final[Path] = REPO / "scripts" / "release.py"
RELEASE_MODULE: Final[str] = "rowx_test_release"
NEW_VERSION: Final[str] = "0.4.3"
NOTES: Final[str] = "- Ships the release command.\n"
HEAD_SHA: Final[str] = "abc123"
OTHER_SHA: Final[str] = "def456"
TREE_FILES: Final[tuple[str, ...]] = (
    "pyproject.toml",
    "Cargo.toml",
    "Cargo.lock",
    "uv.lock",
    "fuzz/Cargo.lock",
    "packages/rowx-core/pyproject.toml",
    "packages/rowx-postgres/pyproject.toml",
    "packages/rowx-embedded/pyproject.toml",
    "packages/rowx-embedded/Cargo.toml",
    "packages/rowx-embedded/crates/rowx-embedded-host/Cargo.toml",
    "docs/changelog.md",
    "docs/packages.md",
    "docs/contributing/decisions.md",
)


class _Plan(Protocol):
    @property
    def action(self) -> str: ...

    @property
    def version(self) -> str: ...


class _Validator(Protocol):
    def __call__(self) -> int: ...


class _Release(Protocol):
    def bump(self, root: Path, new_version: str, notes: str, *, published: bool) -> None: ...

    def tag_plan(self, *, head_version: str, parent_version: str, head_sha: str, tag_sha: str | None) -> _Plan: ...


def _load_release() -> _Release:
    spec = spec_from_file_location(RELEASE_MODULE, SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[RELEASE_MODULE] = module
    spec.loader.exec_module(module)
    return cast("_Release", module)


RELEASE: Final[_Release] = _load_release()


def _copy_tree(destination: Path) -> None:
    for relative in TREE_FILES:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        copy2(REPO / relative, target)


def _without_anchors(text: str, version: str) -> str:
    return text.replace(f"under one version ({version})", "VERSION").replace(f"This tree is version {version}.", "TREE")


def test_bump_updates_lockstep_versions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _copy_tree(tmp_path)
    RELEASE.bump(tmp_path, NEW_VERSION, NOTES, published=True)
    project = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "0.4.3"' in project
    assert "rowx-core==0.4.3" in project
    assert "rowx-postgres==0.4.2" not in project
    assert "rowx-embedded==0.4.2" not in project
    fuzz = (tmp_path / "fuzz/Cargo.lock").read_text(encoding="utf-8")
    assert 'name = "rowx-core"\nversion = "0.4.3"' in fuzz
    assert 'name = "rowx-postgres"\nversion = "0.4.3"' in fuzz
    monkeypatch.setenv("EVENT_NAME", "pull_request")
    monkeypatch.delenv("TAG_NAME", raising=False)
    validator = REPO / "scripts" / "validate-release-versions.py"
    spec = spec_from_file_location("rowx_test_release_validator", validator)
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    main = cast("_Validator", module.__dict__["main"])
    monkeypatch.setattr(sys, "argv", [str(validator), "--root", str(tmp_path)])
    assert main() == 0


def test_bump_rejects_version_that_is_not_greater(tmp_path: Path) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text('[project]\nversion = "0.4.2"\n', encoding="utf-8")
    for version in ("0.4.2", "0.4.1", "0.0.0"):
        with pytest.raises(SystemExit, match="not greater"):
            RELEASE.bump(tmp_path, version, NOTES, published=True)
    for version in ("0.4.3rc1", "v0.4.3", "1.2"):
        with pytest.raises(SystemExit, match="stable"):
            RELEASE.bump(tmp_path, version, NOTES, published=True)
    assert project.read_text(encoding="utf-8") == '[project]\nversion = "0.4.2"\n'


def test_bump_inserts_changelog_section_when_current_is_published(tmp_path: Path) -> None:
    _copy_tree(tmp_path)
    with pytest.raises(SystemExit, match="changelog notes"):
        RELEASE.bump(tmp_path, NEW_VERSION, "", published=True)
    original = (tmp_path / "docs/changelog.md").read_text(encoding="utf-8")
    RELEASE.bump(tmp_path, NEW_VERSION, NOTES, published=True)
    updated = (tmp_path / "docs/changelog.md").read_text(encoding="utf-8")
    assert updated.startswith(f"# Changelog\n\n## {NEW_VERSION}\n\n{NOTES}")
    assert updated.endswith(original[original.index("## 0.4.2") :])


def test_bump_retitles_unpublished_changelog_heading(tmp_path: Path) -> None:
    _copy_tree(tmp_path)
    original = (tmp_path / "docs/changelog.md").read_text(encoding="utf-8")
    note = "- Cut again after the tag failed.\n"
    RELEASE.bump(tmp_path, NEW_VERSION, note, published=False)
    updated = (tmp_path / "docs/changelog.md").read_text(encoding="utf-8")
    assert updated.startswith(f"# Changelog\n\n## {NEW_VERSION}\n\n{note}")
    assert "## 0.4.2\n" not in updated
    assert original.split("## 0.4.2\n", 1)[1] in updated
    assert "v0.4.0" in updated
    assert 'version = "0.4.3"' in (tmp_path / "pyproject.toml").read_text(encoding="utf-8")


def test_bump_leaves_older_changelog_sections(tmp_path: Path) -> None:
    _copy_tree(tmp_path)
    original = (tmp_path / "docs/changelog.md").read_text(encoding="utf-8")
    RELEASE.bump(tmp_path, NEW_VERSION, NOTES, published=True)
    updated = (tmp_path / "docs/changelog.md").read_text(encoding="utf-8")
    assert updated.endswith(original[original.index("## 0.3.0") :])
    assert updated.endswith(original[original.index("## 0.4.2") :])


def test_bump_updates_only_packages_md_anchors(tmp_path: Path) -> None:
    _copy_tree(tmp_path)
    original = (tmp_path / "docs/packages.md").read_text(encoding="utf-8")
    decisions = (tmp_path / "docs/contributing/decisions.md").read_text(encoding="utf-8")
    RELEASE.bump(tmp_path, NEW_VERSION, NOTES, published=True)
    updated = (tmp_path / "docs/packages.md").read_text(encoding="utf-8")
    assert _without_anchors(updated, NEW_VERSION) == _without_anchors(original, "0.4.2")
    assert "under one version (0.4.3)" in updated
    assert "This tree is version 0.4.3." in updated
    assert "0.4.2 added `rowx-embedded`" in updated
    assert (tmp_path / "docs/contributing/decisions.md").read_text(encoding="utf-8") == decisions


def test_tag_plan_skips_when_version_unchanged() -> None:
    plan = RELEASE.tag_plan(head_version="0.4.2", parent_version="0.4.2", head_sha=HEAD_SHA, tag_sha=OTHER_SHA)
    assert plan.action == "skip"


def test_tag_plan_emits_version_when_parent_differs() -> None:
    plan = RELEASE.tag_plan(head_version=NEW_VERSION, parent_version="0.4.2", head_sha=HEAD_SHA, tag_sha=None)
    assert plan.action == "tag"
    assert plan.version == NEW_VERSION
    already = RELEASE.tag_plan(head_version=NEW_VERSION, parent_version="0.4.2", head_sha=HEAD_SHA, tag_sha=HEAD_SHA)
    assert already.action == "skip"


def test_tag_plan_refuses_when_tag_points_elsewhere() -> None:
    with pytest.raises(SystemExit, match="already points at"):
        RELEASE.tag_plan(head_version=NEW_VERSION, parent_version="0.4.2", head_sha=HEAD_SHA, tag_sha=OTHER_SHA)

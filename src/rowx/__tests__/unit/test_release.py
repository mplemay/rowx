import sys
from importlib.util import module_from_spec, spec_from_file_location
from os import environ
from pathlib import Path
from shutil import copy2, which
from subprocess import run  # ruff: ignore[suspicious-subprocess-import]
from typing import Final, Protocol, cast

import pytest

REPO: Final[Path] = Path(__file__).resolve().parents[4]
SCRIPT: Final[Path] = REPO / "scripts" / "release.py"
RELEASE_MODULE: Final[str] = "rowx_test_release"
NEW_VERSION: Final[str] = "0.4.3"
HEAD_SHA: Final[str] = "abc123"
OTHER_SHA: Final[str] = "def456"
RELEASE_DAY: Final[str] = "2026-01-03"
RELEASE_INSTANT: Final[str] = "2026-01-03T12:00:00Z"
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


class _ChangelogPlan(Protocol):
    @property
    def tag(self) -> str | None: ...

    @property
    def rewrite_top_date(self) -> bool: ...


class _Release(Protocol):
    def bump(self, root: Path, new_version: str) -> None: ...

    def tag_plan(self, *, head_version: str, parent_version: str, head_sha: str, tag_sha: str | None) -> _Plan: ...

    def changelog_plan(self, *, version: str, head: str, tag_commit: str | None) -> _ChangelogPlan: ...

    def replace_top_heading_date(self, text: str, day: str) -> str: ...

    def release_committer_date(self, day: str) -> str: ...

    def render_changelog(self, root: Path, *, top_date: str | None = None) -> str: ...

    def _link_open_pull_request(self, text: str, number: int) -> str: ...

    def _pull_request_number(self, root: Path) -> int | None: ...


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
    RELEASE.bump(tmp_path, NEW_VERSION)
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
            RELEASE.bump(tmp_path, version)
    for version in ("0.4.3rc1", "v0.4.3", "1.2"):
        with pytest.raises(SystemExit, match="stable"):
            RELEASE.bump(tmp_path, version)
    assert project.read_text(encoding="utf-8") == '[project]\nversion = "0.4.2"\n'


def test_bump_leaves_the_changelog(tmp_path: Path) -> None:
    _copy_tree(tmp_path)
    original = (tmp_path / "docs/changelog.md").read_text(encoding="utf-8")
    RELEASE.bump(tmp_path, NEW_VERSION)
    assert (tmp_path / "docs/changelog.md").read_text(encoding="utf-8") == original


def test_bump_updates_only_packages_md_anchors(tmp_path: Path) -> None:
    _copy_tree(tmp_path)
    original = (tmp_path / "docs/packages.md").read_text(encoding="utf-8")
    decisions = (tmp_path / "docs/contributing/decisions.md").read_text(encoding="utf-8")
    RELEASE.bump(tmp_path, NEW_VERSION)
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


def test_changelog_plan_skips_the_tag_when_it_points_at_head() -> None:
    plan = RELEASE.changelog_plan(version=NEW_VERSION, head=HEAD_SHA, tag_commit=HEAD_SHA)
    assert plan.tag is None
    assert plan.rewrite_top_date is False


def test_changelog_plan_uses_a_virtual_tag_when_the_tag_is_missing() -> None:
    plan = RELEASE.changelog_plan(version=NEW_VERSION, head=HEAD_SHA, tag_commit=None)
    assert plan.tag == f"v{NEW_VERSION}"
    assert plan.rewrite_top_date is True


def test_changelog_plan_leaves_released_history_when_the_tag_is_older() -> None:
    plan = RELEASE.changelog_plan(version="0.4.2", head=HEAD_SHA, tag_commit=OTHER_SHA)
    assert plan.tag is None
    assert plan.rewrite_top_date is False


def test_replace_top_heading_date_changes_only_the_first_date() -> None:
    text = (
        "# Changelog\n\n"
        "## [0.4.3](https://example.com/compare/v0.4.2...v0.4.3) - 2026-01-01\n\n"
        "## [0.4.2](https://example.com) - 2026-11-16\n"
    )
    updated = RELEASE.replace_top_heading_date(text, RELEASE_DAY)
    assert updated.startswith("# Changelog\n\n## [0.4.3](https://example.com/compare/v0.4.2...v0.4.3) - 2026-01-03\n")
    assert "## [0.4.2](https://example.com) - 2026-11-16\n" in updated


def test_release_committer_date_is_noon_utc() -> None:
    assert RELEASE.release_committer_date(RELEASE_DAY) == RELEASE_INSTANT


UNRELEASED_CHANGELOG: Final[str] = (
    "# Changelog\n\n"
    "## [unreleased]\n\n"
    "### Bug fixes\n\n"
    "- Accept the factored embedded extra marker\n\n"
    "## [0.4.2](https://example.com) - 2026-11-16\n\n"
    "- Drop the deprecated pep 639 license classifier"
    " ([#329](https://github.com/mplemay/rowx/pull/329))\n"
)
LINKED_BULLET: Final[str] = (
    "- Accept the factored embedded extra marker ([#359](https://github.com/mplemay/rowx/pull/359))\n"
)


def test_open_pull_request_links_only_unreleased_bullets() -> None:
    linked = RELEASE._link_open_pull_request(UNRELEASED_CHANGELOG, 359)
    assert LINKED_BULLET in linked
    assert "([#329](https://github.com/mplemay/rowx/pull/329))" in linked
    assert RELEASE._link_open_pull_request(linked, 359) == linked


def test_actions_pull_request_number(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_REF", "refs/pull/359/merge")
    assert RELEASE._pull_request_number(REPO) == 359


def test_push_event_keeps_commit_message_links(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.delenv("GITHUB_REF", raising=False)
    assert RELEASE._pull_request_number(REPO) is None


def test_pull_request_number_ignores_other_repositories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_REF", "refs/pull/359/merge")
    assert RELEASE._pull_request_number(tmp_path) is None


def test_virtual_tag_render_matches_the_tagged_commit(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "docs").mkdir()
    copy2(REPO / "cliff.toml", root / "cliff.toml")
    (root / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.1.0"\n', encoding="utf-8")
    (root / "feature.txt").write_text("one\n", encoding="utf-8")
    _git(root, "init", "-b", "main")
    _commit(root, "feat: add the first feature", "2026-01-01T12:00:00Z")
    _git(root, "tag", "v0.1.0")
    (root / "feature.txt").write_text("two\n", encoding="utf-8")
    _commit(root, "feat: add the next feature", "2026-01-02T12:00:00Z")
    (root / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.1.1"\n', encoding="utf-8")
    before_commit = RELEASE.render_changelog(root, top_date=RELEASE_DAY)
    (root / "docs" / "changelog.md").write_text(before_commit, encoding="utf-8")
    _commit(root, "ci: release 0.1.1", RELEASE_INSTANT)
    before_tag = RELEASE.render_changelog(root)
    assert before_tag == before_commit
    _git(root, "tag", "v0.1.1")
    after_tag = RELEASE.render_changelog(root)
    assert after_tag == before_commit
    assert f" - {RELEASE_DAY}" in after_tag.split("## [0.1.0]", maxsplit=1)[0]


def _git_executable() -> str:
    executable = which("git")
    if executable is None:
        message = "git is required"
        raise AssertionError(message)
    return executable


def _git(root: Path, *args: str) -> str:
    completed = run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [_git_executable(), "-c", "user.email=release-test@example.com", "-c", "user.name=Release Test", *args],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        raise AssertionError(message)
    return completed.stdout


def _commit(root: Path, message: str, date: str) -> None:
    _git(root, "add", "--all")
    env = environ.copy()
    env["GIT_AUTHOR_DATE"] = date
    env["GIT_COMMITTER_DATE"] = date
    completed = run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [
            _git_executable(),
            "-c",
            "user.email=release-test@example.com",
            "-c",
            "user.name=Release Test",
            "commit",
            "-m",
            message,
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise AssertionError(detail)

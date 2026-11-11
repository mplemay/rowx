from pathlib import Path, PurePosixPath
from subprocess import run  # ruff: ignore[suspicious-subprocess-import]
from typing import TYPE_CHECKING

import pytest

from rowx.check.config import Config, ConfigError
from rowx.check.walk import discover_files, is_excluded, validate_pattern

if TYPE_CHECKING:
    from collections.abc import Callable


def touch(root: Path, *names: str) -> None:
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")


def names(root: Path, files: list[Path]) -> list[str]:
    return [file.relative_to(root.resolve()).as_posix() for file in files]


def git(root: Path, *args: str) -> None:
    run(["git", "-c", "user.email=a@b.c", "-c", "user.name=t", *args], cwd=root, check=True, capture_output=True)  # ruff: ignore[subprocess-without-shell-equals-true, start-process-with-partial-path]


@pytest.mark.parametrize(
    ("path", "patterns", "expected"),
    [
        ("a/migrations/001.py", ("**/migrations/**",), True),
        ("migrations/001.py", ("**/migrations/**",), True),
        ("a/b.py", ("**/migrations/**",), False),
        ("a/build/x.py", ("build",), False),
        ("a/build/x.py", ("/build",), False),
        ("a/building/x.py", ("build",), False),
        ("a/gen_x.py", ("gen_*.py",), False),
        ("a/b/c.py", ("a/b",), True),
        ("x/a/b/c.py", ("a/b",), False),
        ("a/b", ("a[!x]b",), False),
    ],
)
def test_is_excluded(path: str, patterns: tuple[str, ...], *, expected: bool) -> None:
    assert is_excluded(PurePosixPath(path), patterns) is expected


def test_venv_excluded_without_gitignore(tmp_path: Path) -> None:
    touch(tmp_path, "app.py", ".venv/lib/site.py", "node_modules/x.py", "pkg/__pycache__/y.py", "pkg/ok.py")
    files = discover_files(Config(root=tmp_path), tmp_path)
    assert names(tmp_path, files) == ["app.py", "pkg/ok.py"]


def test_only_python_files_sorted(tmp_path: Path) -> None:
    touch(tmp_path, "b.py", "a.py", "notes.txt", "sub/c.py")
    files = discover_files(Config(root=tmp_path), tmp_path)
    assert names(tmp_path, files) == ["a.py", "b.py", "sub/c.py"]


def test_include_and_user_exclude(tmp_path: Path) -> None:
    touch(tmp_path, "src/a.py", "src/migrations/m.py", "other/b.py")
    config = Config(root=tmp_path, include=("src",), exclude=("**/migrations/**",))
    assert names(tmp_path, discover_files(config, tmp_path)) == ["src/a.py"]


def test_literal_includes_normalize_dot_segments_for_exclusion_and_deduplication(tmp_path: Path) -> None:
    touch(tmp_path, "src/a.py", "generated/b.py")
    config = Config(root=tmp_path, include=("src", "src/../src", "src/../generated"), exclude=("generated",))
    assert names(tmp_path, discover_files(config, tmp_path)) == ["src/a.py"]


def test_explicit_paths_override_include_and_own_exclusion(tmp_path: Path) -> None:
    touch(tmp_path, "src/a.py", "other/b.py", "other/migrations/m.py", "other/gen.py")
    config = Config(root=tmp_path, include=("src",), exclude=("**/migrations/**", "**/gen.py"))
    files = discover_files(config, tmp_path, [Path("other")])
    assert names(tmp_path, files) == ["other/b.py"]
    explicit_file = discover_files(config, tmp_path, [Path("other/gen.py"), Path("other/b.py")])
    assert names(tmp_path, explicit_file) == ["other/b.py", "other/gen.py"]


def test_explicit_path_relative_to_cwd(tmp_path: Path) -> None:
    touch(tmp_path, "pkg/a.py", "b.py")
    files = discover_files(Config(root=tmp_path), tmp_path / "pkg", [Path("a.py")])
    assert names(tmp_path, files) == ["pkg/a.py"]


def test_missing_explicit_path(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="path does not exist"):
        discover_files(Config(root=tmp_path), tmp_path, [Path("nope")])


def test_gitignore_honoured_through_git(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q")
    touch(tmp_path, "a.py", "ignored/b.py", "tracked.py", "untracked.py")
    (tmp_path / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    git(tmp_path, "add", "tracked.py")
    files = discover_files(Config(root=tmp_path), tmp_path)
    assert names(tmp_path, files) == ["a.py", "tracked.py", "untracked.py"]


def test_exclude_applies_to_git_listing(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q")
    touch(tmp_path, "a.py", ".venv/x.py", "gen/g.py")
    files = discover_files(Config(root=tmp_path, exclude=("gen",)), tmp_path)
    assert names(tmp_path, files) == ["a.py"]


def test_ignore_files_without_git(tmp_path: Path) -> None:
    touch(tmp_path, "a.py", "ignored/b.py", ".venv/c.py")
    (tmp_path / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    files = discover_files(Config(root=tmp_path), tmp_path)
    assert names(tmp_path, files) == ["a.py"]
    assert names(tmp_path, discover_files(Config(root=tmp_path), tmp_path, use_git=False)) == ["a.py", "ignored/b.py"]


def test_parent_named_like_an_exclude_does_not_hide_outside_paths(tmp_path: Path) -> None:
    project = tmp_path / "build" / "proj"
    other = tmp_path / "build" / "elsewhere"
    touch(project, "a.py")
    touch(other, "b.py")
    files = discover_files(Config(root=project), project, [other])
    assert files == [(other / "b.py").resolve()]


def test_ordered_excludes_and_glob_includes(tmp_path: Path) -> None:
    touch(tmp_path, "dist/a.py", "dist/keep.py", "pkg/dist/keep.py", "pkg/src/b.py", "src/a.py")
    config = Config(root=tmp_path, include=("**/dist", "src/*.py"), exclude=("!**/dist/keep.py",))
    assert names(tmp_path, discover_files(config, tmp_path)) == ["dist/keep.py", "pkg/dist/keep.py", "src/a.py"]


def test_nested_ignore_precedence_and_explicit_targets(tmp_path: Path) -> None:
    touch(tmp_path, "pkg/a.py", "pkg/b.py", "pkg/sub/c.py", "hidden/x.py")
    (tmp_path / ".gitignore").write_text("hidden/\npkg/*.py\n", encoding="utf-8")
    (tmp_path / ".ignore").write_text("!pkg/a.py\n", encoding="utf-8")
    (tmp_path / "pkg" / ".ignore").write_text("!b.py\nsub/\n", encoding="utf-8")
    assert names(tmp_path, discover_files(Config(root=tmp_path), tmp_path)) == ["pkg/a.py", "pkg/b.py"]
    assert names(tmp_path, discover_files(Config(root=tmp_path), tmp_path, [Path("hidden/x.py")])) == ["hidden/x.py"]
    assert discover_files(Config(root=tmp_path, force_exclude=True), tmp_path, [Path("hidden/x.py")]) == []
    assert names(tmp_path, discover_files(Config(root=tmp_path, respect_ignore_files=False), tmp_path)) == [
        "hidden/x.py",
        "pkg/a.py",
        "pkg/b.py",
        "pkg/sub/c.py",
    ]


def test_git_hard_exclusion_and_directory_symlinks(tmp_path: Path) -> None:
    touch(tmp_path, ".git/a.py", "real/a.py")
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    config = Config(root=tmp_path, exclude=("!**/.git",))
    assert names(tmp_path, discover_files(config, tmp_path)) == ["real/a.py"]
    assert discover_files(config, tmp_path, [Path(".git")]) == []


@pytest.mark.parametrize("pattern", ["x/***.py", "a**b", "x/[abc", "abc\\"])
def test_invalid_patterns(pattern: str) -> None:
    with pytest.raises(ValueError, match=r".+"):
        validate_pattern(pattern)


def test_explicit_excluded_directory_allows_children(tmp_path: Path) -> None:
    touch(tmp_path, "dist/good.py", "dist/skip.py", "hidden/good.py", "hidden/skip.py")
    (tmp_path / ".gitignore").write_text("hidden/\nhidden/skip.py\n", encoding="utf-8")
    config = Config(root=tmp_path, exclude=("dist/skip.py",))
    assert names(tmp_path, discover_files(config, tmp_path, [Path("dist"), Path("hidden")])) == [
        "dist/good.py",
        "hidden/good.py",
    ]
    assert discover_files(Config(root=tmp_path, force_exclude=True), tmp_path, [Path("dist"), Path("hidden")]) == []


@pytest.mark.parametrize("exclude", [(), ("**/build",), ("**/build", "!**/build/keep.py")])
def test_explicit_directory_keeps_descendant_exclusions(tmp_path: Path, exclude: tuple[str, ...]) -> None:
    root = tmp_path.resolve()
    touch(root, "build/ok.py", "build/nested/build/secret.py", "build/nested/build/keep.py", "build/skip.py")
    config = Config(root=root, exclude=(*exclude, "build/skip.py"))
    expected = (
        ["build/nested/build/keep.py", "build/ok.py"] if exclude and exclude[-1].startswith("!") else ["build/ok.py"]
    )
    assert names(root, discover_files(config, root, [Path("build")])) == expected


@pytest.mark.parametrize("ignore_file", [".gitignore", ".ignore"])
def test_explicit_directory_keeps_descendant_ignore_matches(tmp_path: Path, ignore_file: str) -> None:
    root = tmp_path.resolve()
    touch(root, "build/ok.py", "build/nested/build/secret.py", "build/skip.py")
    (root / ignore_file).write_text("build/\nbuild/skip.py\n", encoding="utf-8")
    assert names(root, discover_files(Config(root=root), root, [Path("build")])) == ["build/ok.py"]


@pytest.mark.parametrize("include", ["link/sub", "link/*", "link/../real/sub", "real/../link/sub"])
def test_directory_include_does_not_cross_symlink(tmp_path: Path, include: str) -> None:
    root = tmp_path.resolve()
    touch(root, "real/sub/a.py")
    (root / "link").symlink_to(root / "real", target_is_directory=True)
    assert discover_files(Config(root=root, include=(include,)), root) == []
    assert discover_files(Config(root=root), root, [Path("link/sub")]) == []
    assert names(root, discover_files(Config(root=root), root, [Path("link/sub/a.py")])) == ["link/sub/a.py"]


def test_literal_file_include_does_not_cross_directory_symlink(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    touch(root, "real/sub/a.py")
    (root / "link").symlink_to(root / "real", target_is_directory=True)
    assert discover_files(Config(root=root, include=("link/sub/a.py",)), root) == []
    assert names(root, discover_files(Config(root=root, force_exclude=True), root, [Path("link/sub/a.py")])) == [
        "link/sub/a.py"
    ]


def test_external_directory_does_not_cross_symlink(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    project = root / "project"
    touch(project, "a.py")
    touch(root, "real/sub/a.py")
    (root / "link").symlink_to(root / "real", target_is_directory=True)
    assert discover_files(Config(root=project), project, [root / "link/sub"]) == []


def test_explicit_directory_respects_wildcard_ignore(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    touch(root, "selected/a.py")
    (root / ".ignore").write_text("*\n", encoding="utf-8")
    assert discover_files(Config(root=root), root, [Path("selected")]) == []


def test_force_exclude_cannot_reopen_ignored_parent(tmp_path: Path) -> None:
    touch(tmp_path, "hidden/a.py")
    (tmp_path / ".gitignore").write_text("hidden/\n!hidden/a.py\n", encoding="utf-8")
    config = Config(root=tmp_path, force_exclude=True)
    assert discover_files(config, tmp_path, [Path("hidden/a.py")]) == []
    assert discover_files(Config(root=tmp_path, include=("hidden/*.py",)), tmp_path) == []


def test_invalid_ignore_file_fails_discovery(tmp_path: Path) -> None:
    touch(tmp_path, "a.py")
    (tmp_path / ".ignore").write_bytes(b"\xff")
    with pytest.raises(ConfigError, match=r"cannot read ignore file .*invalid UTF-8"):
        discover_files(Config(root=tmp_path), tmp_path)


def test_walk_errors_fail_discovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from rowx.check import walk  # ruff: ignore[import-outside-top-level]

    def failed_walk(_path: Path, *, followlinks: bool, onerror: Callable[[OSError], None]) -> list[object]:
        assert followlinks is False
        onerror(PermissionError(13, "permission denied", str(tmp_path / "blocked")))
        return []

    monkeypatch.setattr(walk, "os_walk", failed_walk)
    with pytest.raises(ConfigError, match=r"cannot read directory .*permission denied"):
        discover_files(Config(root=tmp_path), tmp_path)

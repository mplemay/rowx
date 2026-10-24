from pathlib import Path, PurePosixPath
from subprocess import run

import pytest

from rowx.check import walk
from rowx.check.config import Config, ConfigError
from rowx.check.walk import discover_files, is_excluded


def touch(root: Path, *names: str) -> None:
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")


def names(root: Path, files: list[Path]) -> list[str]:
    return [file.relative_to(root.resolve()).as_posix() for file in files]


def git(root: Path, *args: str) -> None:
    run(["git", "-c", "user.email=a@b.c", "-c", "user.name=t", *args], cwd=root, check=True, capture_output=True)  # noqa: S603, S607


@pytest.mark.parametrize(
    ("path", "patterns", "expected"),
    [
        ("a/migrations/001.py", ("**/migrations/**",), True),
        ("migrations/001.py", ("**/migrations/**",), True),
        ("a/b.py", ("**/migrations/**",), False),
        ("a/build/x.py", ("build",), True),
        ("a/building/x.py", ("build",), False),
        ("a/gen_x.py", ("gen_*.py",), True),
        ("a/b/c.py", ("a/b",), True),
        ("x/a/b/c.py", ("a/b",), False),
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


def test_explicit_paths_override_include_but_honour_exclude(tmp_path: Path) -> None:
    touch(tmp_path, "src/a.py", "other/b.py", "other/migrations/m.py", "other/gen.py")
    config = Config(root=tmp_path, include=("src",), exclude=("**/migrations/**", "gen.py"))
    files = discover_files(config, tmp_path, [Path("other")])
    assert names(tmp_path, files) == ["other/b.py"]
    explicit_file = discover_files(config, tmp_path, [Path("other/gen.py"), Path("other/b.py")])
    assert names(tmp_path, explicit_file) == ["other/b.py"]


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


def test_falls_back_to_globs_without_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    touch(tmp_path, "a.py", "ignored/b.py", ".venv/c.py")
    (tmp_path / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    monkeypatch.setattr(walk, "_git_files", lambda _root: None)
    files = discover_files(Config(root=tmp_path), tmp_path)
    assert names(tmp_path, files) == ["a.py", "ignored/b.py"]


def test_parent_named_like_an_exclude_does_not_hide_outside_paths(tmp_path: Path) -> None:
    project = tmp_path / "build" / "proj"
    other = tmp_path / "build" / "elsewhere"
    touch(project, "a.py")
    touch(other, "b.py")
    files = discover_files(Config(root=project), project, [other])
    assert files == [(other / "b.py").resolve()]

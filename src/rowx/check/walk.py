from fnmatch import translate
from functools import cache
from glob import translate as glob_translate
from os import walk as os_walk
from pathlib import Path, PurePosixPath
from re import DOTALL, Pattern, compile as re_compile
from subprocess import DEVNULL, CalledProcessError, run  # ruff: ignore[suspicious-subprocess-import]
from typing import Final

from rowx.check.config import Config, ConfigError

SOURCE_SUFFIX: Final[str] = ".py"


@cache
def _matchers(patterns: tuple[str, ...]) -> tuple[Pattern[str], Pattern[str]]:
    # a glob without a slash matches a name at any depth; every glob also matches the root-relative path
    names: list[str] = []
    paths: list[str] = []
    for raw in patterns:
        pattern = raw.strip("/")
        if "/" not in pattern:
            names.append(translate(pattern))
        paths.append(glob_translate(pattern, recursive=True, include_hidden=True))
    never = r"(?!)"
    return re_compile("|".join(names) or never, DOTALL), re_compile("|".join(paths) or never, DOTALL)


def is_excluded(relative: PurePosixPath, patterns: tuple[str, ...], *, is_dir: bool = False) -> bool:
    by_name, by_path = _matchers(patterns)
    parts = relative.parts
    for index, part in enumerate(parts):
        if by_name.match(part):
            return True
        prefix = PurePosixPath(*parts[: index + 1]).as_posix()
        last = index == len(parts) - 1
        if by_path.match(prefix) or (by_path.match(prefix + "/") and (is_dir or not last)):
            return True
    return False


def _relative(path: Path, root: Path, anchor: Path) -> PurePosixPath:
    # outside the project root only the part below the explicit path is matched, so a parent
    # directory called `build` doesn't exclude everything under it
    base = root if path.is_relative_to(root) else anchor
    return PurePosixPath(path.relative_to(base).as_posix())


def _git_files(root: Path) -> set[Path] | None:
    try:
        completed = run(
            ["git", "ls-files", "-z", "--others", "--exclude-standard", "--cached"],  # ruff: ignore[start-process-with-partial-path]
            cwd=root,
            check=True,
            capture_output=True,
            stdin=DEVNULL,
        )
    except OSError, CalledProcessError:
        return None
    names = completed.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    return {root / name for name in names if name}


def _walk_dir(directory: Path, root: Path, patterns: tuple[str, ...]) -> list[Path]:
    found: list[Path] = []
    for current, dirnames, filenames in os_walk(directory):
        here = Path(current)
        dirnames[:] = sorted(
            d for d in dirnames if not is_excluded(_relative(here / d, root, directory), patterns, is_dir=True)
        )
        found.extend(
            here / name
            for name in sorted(filenames)
            if name.endswith(SOURCE_SUFFIX) and not is_excluded(_relative(here / name, root, directory), patterns)
        )
    return found


def discover_files(config: Config, cwd: Path, paths: list[Path] | None = None) -> list[Path]:
    root = config.root
    patterns = config.all_excludes
    if paths is not None and paths:
        targets = [(cwd / path).resolve() for path in paths]
    else:
        targets = [(root / entry).resolve() for entry in config.include]
    tracked = _git_files(root)
    found: dict[Path, None] = {}
    for target in targets:
        if not target.exists():
            msg = f"path does not exist: {target}"
            raise ConfigError(msg)
        if target.is_file():
            anchor = target.parent
            if not is_excluded(_relative(target, root, anchor), patterns):
                found[target] = None
            continue
        if tracked is None or not target.is_relative_to(root):
            candidates = _walk_dir(target, root, patterns)
        else:
            candidates = sorted(
                path
                for path in tracked
                if path.suffix == SOURCE_SUFFIX
                and path.is_relative_to(target)
                and path.is_file()
                and not is_excluded(_relative(path, root, target), patterns)
            )
        found.update(dict.fromkeys(candidates))
    return sorted(found)

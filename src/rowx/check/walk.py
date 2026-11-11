from fnmatch import translate
from functools import cache
from os import walk as os_walk
from os.path import abspath
from pathlib import Path, PurePosixPath
from re import Pattern, compile as re_compile, escape
from typing import Final

from pathspec import GitIgnoreSpec

from rowx.check.config import DEFAULT_EXCLUDE, Config, ConfigError

SOURCE_SUFFIX: Final[str] = ".py"


def _class_end(pattern: str, index: int) -> int:
    end = index + 1
    if pattern[end : end + 1] in {"!", "^"}:
        end += 1
    if pattern[end : end + 1] == "]":
        end += 1
    while end < len(pattern) and pattern[end] != "]":
        end += 1
    if end == len(pattern):
        msg = "unclosed character class"
        raise ValueError(msg)
    return end


def validate_pattern(pattern: str, *, allow_negation: bool = False) -> None:
    if pattern.startswith("!"):
        if not allow_negation:
            msg = "negation is only allowed in excludes"
            raise ValueError(msg)
        pattern = pattern[1:]
    if not pattern:
        msg = "empty pattern"
        raise ValueError(msg)
    index = 0
    while index < len(pattern):
        if pattern[index] == "\\":
            index += 1
            if index == len(pattern):
                msg = "trailing escape"
                raise ValueError(msg)
        elif pattern[index] == "[":
            index = _class_end(pattern, index)
        elif pattern[index : index + 2] == "**":
            before = index == 0 or pattern[index - 1] == "/"
            after = index + 2 == len(pattern) or pattern[index + 2] == "/"
            if not before or not after:
                msg = "** must be a complete path component"
                raise ValueError(msg)
            index += 1
        index += 1


def _normalize(pattern: str) -> str:
    pattern = pattern.removeprefix("./")
    return pattern.lstrip("/").rstrip("/")


def literal_prefix(pattern: str) -> Path:
    parts: list[str] = []
    for component in _normalize(pattern).split("/"):
        literal = ""
        index = 0
        while index < len(component):
            char = component[index]
            if char in "*?[":
                return Path(*parts)
            if char == "\\":
                index += 1
                char = component[index]
            literal += char
            index += 1
        if literal:
            parts.append(literal)
    return Path(*parts)


@cache
def _matcher(pattern: str) -> Pattern[str]:
    pattern = _normalize(pattern)
    result = ""
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            index += 1
            result += escape(pattern[index])
        elif pattern[index : index + 2] == "**":
            index += 1
            if index + 1 < len(pattern) and pattern[index + 1] == "/":
                result += "(?:[^/]+/)*"
                index += 1
            else:
                result += ".*"
        elif char == "*":
            result += "[^/]*"
        elif char == "?":
            result += "[^/]"
        elif char == "[":
            end = _class_end(pattern, index)
            result += "(?!/)" + translate(pattern[index : end + 1])[4:-3]
            index = end
        else:
            result += escape(char)
        index += 1
    return re_compile(f"^{result}$")


def _matches(relative: PurePosixPath, pattern: str, *, is_dir: bool, after: int = 0) -> bool:
    directory_only = pattern.endswith("/")
    matcher = _matcher(pattern)
    parts = relative.parts
    return any(
        matcher.fullmatch(PurePosixPath(*parts[:index]).as_posix())
        and (not directory_only or index < len(parts) or is_dir)
        for index in range(after + 1, len(parts) + 1)
    )


def is_excluded(relative: PurePosixPath, patterns: tuple[str, ...], *, is_dir: bool = False) -> bool:
    excluded = False
    for pattern in patterns:
        negated = pattern.startswith("!")
        if _matches(relative, pattern[1:] if negated else pattern, is_dir=is_dir):
            excluded = not negated
    return excluded


def _relative(path: Path, root: Path, anchor: Path) -> PurePosixPath:
    base = root if path.is_relative_to(root) else anchor
    return PurePosixPath(path.relative_to(base).as_posix())


def _walk_error(error: OSError) -> None:
    msg = f"cannot read directory {error.filename}: {error.strerror or 'I/O error'}"
    raise ConfigError(msg) from error


def _read_ignore(path: Path) -> GitIgnoreSpec | None:
    try:
        return GitIgnoreSpec.from_lines(path.read_text(encoding="utf-8").splitlines())
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        reason = "invalid UTF-8" if isinstance(error, UnicodeDecodeError) else error.strerror or "I/O error"
        msg = f"cannot read ignore file {path}: {reason}"
        raise ConfigError(msg) from error


class _Discovery:
    def __init__(self, config: Config, *, use_git: bool) -> None:
        self.root = config.root.resolve()
        self.patterns = (*(f"**/{name}" for name in DEFAULT_EXCLUDE), *config.exclude)
        self.respect_ignores = use_git and config.respect_ignore_files
        self.found: set[Path] = set()
        self.specs: dict[Path, GitIgnoreSpec | None] = {}
        self.bypass_dir: Path | None = None

    def ignored(self, path: Path, anchor: Path, *, is_dir: bool) -> bool:
        base = self.root if path.is_relative_to(self.root) else anchor
        if not self.respect_ignores or path == base:
            return False
        ignored = False
        for parent in reversed((path.parent, *path.parent.relative_to(base).parents)):
            folder = parent if parent.is_absolute() else base / parent
            for name in (".gitignore", ".ignore"):
                file = folder / name
                if file not in self.specs:
                    self.specs[file] = _read_ignore(file)
                spec = self.specs[file]
                if spec is None:
                    continue
                if self.bypass_dir is not None and self.bypass_dir.is_relative_to(folder):
                    parts = path.relative_to(folder).parts
                    start = len(self.bypass_dir.relative_to(folder).parts)
                    candidates = [
                        PurePosixPath(*parts[:index]).as_posix() + ("/" if index < len(parts) or is_dir else "")
                        for index in range(start + 1, len(parts) + 1)
                    ]
                    # Ignore inherited matches at the selected directory, but retain
                    # the same pattern when it independently matches a descendant.
                    spec = GitIgnoreSpec(
                        pattern
                        for pattern in spec.patterns
                        if any(
                            (match := pattern.match_file(candidate)) is not None
                            and (
                                match.match.groupdict().get("ps_d") is None
                                or match.match.start("ps_d") == len(candidate) - 1
                            )
                            for candidate in candidates
                        )
                    )
                relative = path.relative_to(folder).as_posix() + ("/" if is_dir else "")
                result = spec.check_file(relative)
                if result.include is not None:
                    ignored = result.include
        return ignored

    def excluded(self, path: Path, anchor: Path, *, is_dir: bool) -> bool:
        if ".git" in path.parts:
            return True
        relative = _relative(path, self.root, anchor)
        after = len(_relative(self.bypass_dir, self.root, anchor).parts) if self.bypass_dir is not None else 0
        excluded = False
        for pattern in self.patterns:
            negated = pattern.startswith("!")
            if _matches(relative, pattern.removeprefix("!"), is_dir=is_dir, after=after):
                excluded = not negated
        return excluded or self.ignored(path, anchor, is_dir=is_dir)

    def glob_targets(self, entry: str) -> list[Path]:
        found: list[Path] = []
        may_reinclude = any(pattern.startswith("!") for pattern in self.patterns)
        for current, dirnames, filenames in os_walk(self.root, followlinks=False, onerror=_walk_error):
            here = Path(current)
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name != ".git"
                and not (here / name).is_symlink()
                and not self.ignored(here / name, self.root, is_dir=True)
                and (may_reinclude or not self.excluded(here / name, self.root, is_dir=True))
            )
            for name in (*dirnames, *sorted(filenames)):
                path = here / name
                if _matcher(entry).fullmatch(path.relative_to(self.root).as_posix()):
                    found.append(path)
        return found

    def visit(self, target: Path, *, bypass: bool, explicit: bool) -> None:
        if not target.exists():
            msg = f"path does not exist: {target}"
            raise ConfigError(msg)
        anchor = target.parent if target.is_file() else target
        if ".git" in target.parts:
            return
        if target.is_dir() or not explicit:
            base = self.root if target.is_relative_to(self.root) else Path(target.anchor)
            if any(
                parent.is_symlink()
                for parent in ((target, *target.parents) if target.is_dir() else target.parents)
                if parent != base and parent.is_relative_to(base)
            ):
                return
        base = self.root if target.is_relative_to(self.root) else anchor
        if not bypass and any(
            self.ignored(parent, anchor, is_dir=True)
            for parent in target.parents
            if parent != base and parent.is_relative_to(base)
        ):
            return
        if (
            not bypass
            and self.excluded(target, anchor, is_dir=target.is_dir())
            and (
                not target.is_dir()
                or self.ignored(target, anchor, is_dir=True)
                or not any(pattern.startswith("!") for pattern in self.patterns)
            )
        ):
            return
        if target.is_file():
            if target.suffix == SOURCE_SUFFIX:
                self.found.add(target)
        elif not target.is_symlink():
            self.bypass_dir = target if bypass else None
            self.walk(target, anchor)
            self.bypass_dir = None

    def walk(self, target: Path, anchor: Path) -> None:
        for current, dirnames, filenames in os_walk(target, followlinks=False, onerror=_walk_error):
            here = Path(current)
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name != ".git"
                and not (here / name).is_symlink()
                and not self.ignored(here / name, anchor, is_dir=True)
                and (
                    any(pattern.startswith("!") for pattern in self.patterns)
                    or not self.excluded(here / name, anchor, is_dir=True)
                )
            )
            for name in filenames:
                path = here / name
                if name.endswith(SOURCE_SUFFIX) and not self.excluded(path, anchor, is_dir=False):
                    self.found.add(path)


def discover_files(config: Config, cwd: Path, paths: list[Path] | None = None, *, use_git: bool = True) -> list[Path]:
    discovery = _Discovery(config, use_git=use_git)
    if bool(paths):
        for path in paths or []:
            # Normalize dot segments without resolving directory symlinks.
            target = Path(abspath(cwd.resolve() / path))  # ruff: ignore[os-path-abspath]
            configured_root = Path(abspath(config.root))  # ruff: ignore[os-path-abspath]
            if target.is_relative_to(configured_root):
                target = discovery.root / target.relative_to(configured_root)
            elif len(target.parts) > 1:
                # System path aliases such as /tmp are not traversal through a
                # symlink selected inside the project or an external target.
                top = Path(*target.parts[:2])
                target = top.resolve() / target.relative_to(top)
            discovery.visit(target, bypass=not config.force_exclude, explicit=True)
    else:
        for entry in config.include:
            normalized = _normalize(entry)
            if not any(char in entry for char in "*?[\\"):
                targets = [discovery.root / normalized if normalized else discovery.root]
            else:
                targets = discovery.glob_targets(entry)
            for target in targets:
                discovery.visit(target, bypass=False, explicit=False)
    return sorted(discovery.found)

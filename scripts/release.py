#!/usr/bin/env python3
from argparse import ArgumentParser
from dataclasses import dataclass
from json import JSONDecodeError, loads
from os import environ
from pathlib import Path
from re import MULTILINE, compile as compile_regex, escape
from shutil import which
from subprocess import run  # ruff: ignore[suspicious-subprocess-import]
from tomllib import TOMLDecodeError, loads as load_toml
from typing import Final, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT: Final[Path] = Path(__file__).resolve().parent.parent
PYPI_RELEASES: Final[str] = "https://pypi.org/pypi/rowx/json"
PYPI_TIMEOUT: Final[float] = 30
MAIN_BRANCH: Final[str] = "main"
SKIP_ACTION: Final = "skip"
TAG_ACTION: Final = "tag"
VERSION_PATTERN: Final = compile_regex(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
HEADING: Final = compile_regex(r"^## (.+)$", flags=MULTILINE)
VERSION_LINE_FILES: Final[tuple[str, ...]] = (
    "pyproject.toml",
    "packages/rowx-core/pyproject.toml",
    "packages/rowx-postgres/pyproject.toml",
    "packages/rowx-embedded/pyproject.toml",
    "Cargo.toml",
    "packages/rowx-embedded/Cargo.toml",
    "packages/rowx-embedded/crates/rowx-embedded-host/Cargo.toml",
)
PIN_COUNTS: Final[dict[str, tuple[tuple[str, int], ...]]] = {
    "pyproject.toml": (("rowx-core", 1), ("rowx-postgres", 3), ("rowx-embedded", 2)),
    "packages/rowx-postgres/pyproject.toml": (("rowx-core", 1),),
    "packages/rowx-embedded/pyproject.toml": (("rowx-core", 1), ("rowx-postgres", 1)),
}
CARGO_PACKAGES: Final[frozenset[str]] = frozenset({
    "rowx-core",
    "rowx-postgres",
    "rowx-postgres-py",
    "rowx-py-core",
    "rowx-embedded",
    "rowx-embedded-host",
})
UV_PACKAGES: Final[frozenset[str]] = frozenset({"rowx", "rowx-core", "rowx-postgres", "rowx-embedded"})
FUZZ_PACKAGES: Final[frozenset[str]] = frozenset({"rowx-core", "rowx-postgres"})
LOCK_FILES: Final[tuple[tuple[str, frozenset[str]], ...]] = (
    ("Cargo.lock", CARGO_PACKAGES),
    ("uv.lock", UV_PACKAGES),
    ("fuzz/Cargo.lock", FUZZ_PACKAGES),
)
RELEASE_PATHS: Final[tuple[str, ...]] = (
    *VERSION_LINE_FILES,
    "Cargo.lock",
    "uv.lock",
    "fuzz/Cargo.lock",
    "docs/changelog.md",
    "docs/packages.md",
)
PACKAGE_MARKER: Final[str] = "[[package]]\n"
COMMIT_MESSAGE: Final[str] = "ci: release rowx, rowx-core, rowx-postgres and rowx-embedded as {version}"

type Version = tuple[int, int, int]
type Action = Literal["skip", "tag"]


@dataclass(frozen=True, slots=True, kw_only=True)
class TagPlan:
    action: Action
    version: str = ""


def main() -> int:
    parser = ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    bump_command = commands.add_parser("bump")
    bump_command.add_argument("version")
    bump_command.add_argument("notes", nargs="?")
    commands.add_parser("tag-plan")
    args = parser.parse_args()
    if args.command == "bump":
        notes = args.notes if isinstance(args.notes, str) else None
        return command_bump(args.version, notes)
    return command_tag_plan()


def command_bump(version: str, notes_path: str | None) -> int:
    _require_stable(version)
    current = _project_version(ROOT / "pyproject.toml")
    _require_greater(current, version)
    notes = _read_notes(notes_path)
    _require_clean_main(ROOT)
    _require_new_branch(ROOT, version)
    published = _published(current)
    try:
        bump(ROOT, version, notes, published=published)
        refresh_locks(ROOT)
        _validate_versions(ROOT)
    except SystemExit:
        _restore_release_files(ROOT)
        raise
    _commit_and_open_pr(ROOT, current, version)
    return 0


def command_tag_plan() -> int:
    head_version = _project_version(ROOT / "pyproject.toml")
    parent_version = _project_version_text(_git(ROOT, "show", "HEAD^:pyproject.toml"), "HEAD^:pyproject.toml")
    head_sha = _git(ROOT, "rev-parse", "HEAD").strip()
    tag_sha = None if head_version == parent_version else _remote_tag(ROOT, head_version)
    plan = tag_plan(
        head_version=head_version,
        parent_version=parent_version,
        head_sha=head_sha,
        tag_sha=tag_sha,
    )
    _write_github_output(plan)
    return 0


def bump(root: Path, new_version: str, notes: str, *, published: bool) -> None:
    _require_stable(new_version)
    current = _project_version(root / "pyproject.toml")
    _require_greater(current, new_version)
    updates = _collect(root, current, new_version, notes, published=published)
    for path, text in updates.items():
        path.write_text(text, encoding="utf-8")


def tag_plan(*, head_version: str, parent_version: str, head_sha: str, tag_sha: str | None) -> TagPlan:
    if head_version == parent_version or tag_sha == head_sha:
        return TagPlan(action=SKIP_ACTION)
    if tag_sha is not None:
        message = f"v{head_version} already points at {tag_sha}"
        raise SystemExit(message)
    return TagPlan(action=TAG_ACTION, version=head_version)


def refresh_locks(root: Path) -> None:
    cargo = _executable("cargo")
    _run([cargo, "metadata", "--locked", "--format-version", "1", "--no-deps"], root)
    _run(
        [cargo, "metadata", "--locked", "--format-version", "1", "--no-deps", "--manifest-path", "fuzz/Cargo.toml"],
        root,
    )
    completed = run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [_executable("uv"), "lock", "--check", "--offline"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        message = f"uv lock --check --offline failed: {detail}"
        raise SystemExit(message)


def _collect(root: Path, current: str, new: str, notes: str, *, published: bool) -> dict[Path, str]:
    updates: dict[Path, str] = {
        root / "docs/changelog.md": _update_changelog(
            _read(root / "docs/changelog.md"), current, new, notes, published=published
        ),
        root / "docs/packages.md": _update_packages(_read(root / "docs/packages.md"), current, new),
    }
    for relative in VERSION_LINE_FILES:
        path = root / relative
        updates[path] = _replace_count(_read(path), f'version = "{current}"', f'version = "{new}"', 1, relative)
    for relative, pins in PIN_COUNTS.items():
        path = root / relative
        text = updates[path]
        for name, count in pins:
            text = _replace_count(text, f"{name}=={current}", f"{name}=={new}", count, f"{relative} {name}")
        updates[path] = text
    for relative, packages in LOCK_FILES:
        updates[root / relative] = _bump_lock(_read(root / relative), packages, current, new)
    return updates


def _update_changelog(text: str, current: str, new: str, notes: str, *, published: bool) -> str:
    match = HEADING.search(text)
    found = match.group(1) if match is not None else "none"
    if match is None or found != current:
        message = f"changelog must open on {current}, found {found}"
        raise SystemExit(message)
    body = notes.strip()
    if published and not body:
        message = "a published release needs changelog notes"
        raise SystemExit(message)
    if body and not any(line.startswith("- ") for line in body.splitlines()):
        message = "changelog notes need a bullet"
        raise SystemExit(message)
    if published:
        return text[: match.start()] + f"## {new}\n\n{body}\n\n" + text[match.start() :]
    heading = f"## {new}"
    renamed = text[: match.start()] + heading + text[match.end() :]
    if not body:
        return renamed
    insert_at = match.start() + len(heading)
    return renamed[:insert_at] + f"\n\n{body}\n" + renamed[insert_at:]


def _update_packages(text: str, current: str, new: str) -> str:
    for old, updated in (
        (f"under one version ({current})", f"under one version ({new})"),
        (f"This tree is version {current}.", f"This tree is version {new}."),
    ):
        text = _replace_count(text, old, updated, 1, "docs/packages.md")
    return text


def _bump_lock(text: str, required: frozenset[str], old: str, new: str) -> str:
    pieces = text.split(PACKAGE_MARKER)
    found: set[str] = set()
    updated = [pieces[0]]
    updated.extend(_bump_lock_stanza(piece, required, old, new, found) for piece in pieces[1:])
    missing = required - found
    if missing:
        message = f"lock is missing {', '.join(sorted(missing))}"
        raise SystemExit(message)
    return PACKAGE_MARKER.join(updated)


def _bump_lock_stanza(piece: str, required: frozenset[str], old: str, new: str, found: set[str]) -> str:
    name = _lock_field(piece, "name")
    if name not in required:
        return piece
    version = _lock_field(piece, "version")
    if version != old:
        message = f"{name} lock version is {version}, expected {old}"
        raise SystemExit(message)
    needle = f'version = "{old}"\n'
    if piece.count(needle) != 1:
        message = f"{name} lock stanza has no single version line"
        raise SystemExit(message)
    found.add(name)
    return piece.replace(needle, f'version = "{new}"\n', 1)


def _lock_field(stanza: str, field: str) -> str:
    prefix = f'{field} = "'
    for line in stanza.splitlines():
        if line.startswith(prefix) and line.endswith('"'):
            return line[len(prefix) : -1]
    message = f"lock stanza is missing {field}"
    raise SystemExit(message)


def _replace_count(text: str, old: str, new: str, count: int, label: str) -> str:
    # a following digit would be a different version, such as 0.4.2 inside 0.4.20
    pattern = compile_regex(escape(old) + r"(?!\d)")
    found = len(pattern.findall(text))
    if found != count:
        message = f"{label}: expected {count} of {old}, found {found}"
        raise SystemExit(message)
    return pattern.sub(new, text)


def _require_stable(version: str) -> Version:
    match = VERSION_PATTERN.fullmatch(version)
    if match is None:
        message = f"{version} is not a stable X.Y.Z"
        raise SystemExit(message)
    major_text, minor_text, patch_text = match.groups()
    return (int(major_text), int(minor_text), int(patch_text))


def _require_greater(current: str, new: str) -> None:
    if _require_stable(new) <= _require_stable(current):
        message = f"{new} is not greater than {current}"
        raise SystemExit(message)


def _project_version(path: Path) -> str:
    return _project_version_text(_read(path), path.name)


def _project_version_text(text: str, label: str) -> str:
    try:
        loaded = load_toml(text)
    except TOMLDecodeError as error:
        message = f"{label}: {error}"
        raise SystemExit(message) from None
    project = loaded.get("project")
    if not isinstance(project, dict):
        message = f"{label} is missing project"
        raise SystemExit(message)
    version = project.get("version")
    if not isinstance(version, str):
        message = f"{label} project.version is not a string"
        raise SystemExit(message)
    return version


def _read(path: Path) -> str:
    if not path.is_file():
        message = f"{path} is missing"
        raise SystemExit(message)
    return path.read_text(encoding="utf-8")


def _read_notes(path: str | None) -> str:
    if not path:
        return ""
    note_path = Path(path)
    if not note_path.is_file():
        message = f"notes file {note_path} does not exist"
        raise SystemExit(message)
    return note_path.read_text(encoding="utf-8")


def _published(version: str) -> bool:
    request = Request(PYPI_RELEASES, headers={"User-Agent": "rowx-release"})
    try:
        with urlopen(request, timeout=PYPI_TIMEOUT) as response:  # ruff: ignore[suspicious-url-open-usage]
            payload = loads(response.read().decode())
    except (HTTPError, URLError, TimeoutError, JSONDecodeError) as error:
        message = f"could not read PyPI releases: {error}"
        raise SystemExit(message) from None
    if not isinstance(payload, dict):
        message = "PyPI response is not an object"
        raise SystemExit(message)
    releases = payload.get("releases")
    if not isinstance(releases, dict):
        message = "PyPI response has no releases"
        raise SystemExit(message)
    return version in releases


def _validate_versions(root: Path) -> None:
    env = environ.copy()
    env["EVENT_NAME"] = "workflow_dispatch"
    env.pop("TAG_NAME", None)
    _run([_executable("python3"), str(root / "scripts" / "validate-release-versions.py")], root, env=env)


def _require_clean_main(root: Path) -> None:
    if _git(root, "status", "--porcelain").strip():
        message = "worktree is dirty"
        raise SystemExit(message)
    if _git(root, "branch", "--show-current").strip() != MAIN_BRANCH:
        message = "release cuts start from main"
        raise SystemExit(message)
    _git(root, "fetch", "origin", MAIN_BRANCH)
    if _git(root, "rev-parse", "HEAD").strip() != _git(root, "rev-parse", "origin/main").strip():
        message = "main is not origin/main"
        raise SystemExit(message)


def _require_new_branch(root: Path, version: str) -> None:
    branch = f"release/{version}"
    git = _executable("git")
    local = run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [git, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if local.returncode == 0:
        message = f"{branch} already exists"
        raise SystemExit(message)
    if _git(root, "ls-remote", "origin", f"refs/heads/{branch}").strip():
        message = f"{branch} already exists on origin"
        raise SystemExit(message)


def _restore_release_files(root: Path) -> None:
    _git(root, "restore", "--source=HEAD", "--worktree", "--staged", "--", *RELEASE_PATHS)


def _commit_and_open_pr(root: Path, old: str, new: str) -> None:
    branch = f"release/{new}"
    message = COMMIT_MESSAGE.format(version=new)
    _git(root, "checkout", "-b", branch)
    _git(root, "add", "--", *RELEASE_PATHS)
    _git(root, "commit", "-m", message)
    _git(root, "push", "-u", "origin", branch)
    _run(
        [_executable("gh"), "pr", "create", "--title", message, "--body", _pull_request_body(old, new)],
        root,
    )


def _pull_request_body(old: str, new: str) -> str:
    return (
        "## Summary\n"
        f"- Bumps every lockstep distribution from {old} to {new}.\n"
        "- The changelog notes are in `docs/changelog.md`.\n"
        "\n"
        f"Merging to main tags `v{new}` after CI is green. That tag runs Release, which publishes.\n"
        "\n"
        "## Test plan\n"
        "- [ ] Review the changelog notes\n"
        "- [ ] CI passes the lockstep version check\n"
    )


def _remote_tag(root: Path, version: str) -> str | None:
    return _tag_commit(_git(root, "ls-remote", "origin", f"refs/tags/v{version}"))


def _tag_commit(output: str) -> str | None:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return None
    peeled = [line for line in lines if line.endswith("^{}")]
    chosen = peeled[-1] if peeled else lines[0]
    sha, separator, _ref = chosen.partition("\t")
    if not separator or not sha:
        message = f"unexpected tag listing {chosen}"
        raise SystemExit(message)
    return sha


def _write_github_output(plan: TagPlan) -> None:
    if (path := environ.get("GITHUB_OUTPUT")) is None:
        return
    lines = [f"action={plan.action}\n"]
    if plan.action == TAG_ACTION:
        lines.extend((f"version={plan.version}\n", f"tag=v{plan.version}\n"))
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.writelines(lines)


def _git(root: Path, *args: str) -> str:
    return _run([_executable("git"), *args], root)


def _run(argv: list[str], root: Path, *, env: dict[str, str] | None = None) -> str:
    completed = run(  # ruff: ignore[subprocess-without-shell-equals-true]
        argv,
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        message = f"{Path(argv[0]).name} failed: {detail}"
        raise SystemExit(message)
    return completed.stdout


def _executable(name: str) -> str:
    if executable := which(name):
        return executable
    message = f"{name} is required"
    raise SystemExit(message)


if __name__ == "__main__":
    raise SystemExit(main())

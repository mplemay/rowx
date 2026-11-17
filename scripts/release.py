#!/usr/bin/env python3
from argparse import ArgumentParser
from dataclasses import dataclass
from datetime import UTC, datetime
from os import environ
from pathlib import Path
from re import MULTILINE, compile as compile_regex, escape
from shutil import which
from subprocess import run  # ruff: ignore[suspicious-subprocess-import]
from tempfile import TemporaryDirectory
from tomllib import TOMLDecodeError, loads as load_toml
from typing import Final, Literal

ROOT: Final[Path] = Path(__file__).resolve().parent.parent
MAIN_BRANCH: Final[str] = "main"
SKIP_ACTION: Final = "skip"
TAG_ACTION: Final = "tag"
VERSION_PATTERN: Final = compile_regex(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
DAY: Final = compile_regex(r"^\d{4}-\d{2}-\d{2}$")
# the first dated release heading; later headings keep the dates git-cliff gave their tags
HEADING_DATE: Final = compile_regex(r"^(## \[[^\]]+\].*) - \d{4}-\d{2}-\d{2}$", flags=MULTILINE)
# noon UTC stays on the same calendar day in the timezones git-cliff and GitHub Actions use
RELEASE_COMMIT_TIME: Final[str] = "T12:00:00Z"
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


@dataclass(frozen=True, slots=True, kw_only=True)
class ChangelogPlan:
    tag: str | None
    rewrite_top_date: bool
    # the current release tag's commit; git-cliff stops there, so later commits stay out of the file
    through: str | None


def main() -> int:
    parser = ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    bump_command = commands.add_parser("bump")
    bump_command.add_argument("version")
    commands.add_parser("tag-plan")
    render_command = commands.add_parser("render-changelog")
    render_command.add_argument("--top-date")
    render_command.add_argument("-o", "--output", required=True)
    args = parser.parse_args()
    if args.command == "bump":
        return command_bump(args.version)
    if args.command == "render-changelog":
        top_date = args.top_date if isinstance(args.top_date, str) else None
        output = args.output if isinstance(args.output, str) else ""
        return command_render_changelog(output, top_date)
    return command_tag_plan()


def command_bump(version: str) -> int:
    _require_stable(version)
    current = _project_version(ROOT / "pyproject.toml")
    _require_greater(current, version)
    _require_clean_main(ROOT)
    _require_new_branch(ROOT, version)
    day = datetime.now(UTC).date().isoformat()
    try:
        bump(ROOT, version)
        refresh_locks(ROOT)
        _validate_versions(ROOT)
        changelog = render_changelog(ROOT, top_date=day)
        (ROOT / "docs" / "changelog.md").write_text(changelog, encoding="utf-8")
    except SystemExit:
        _restore_release_files(ROOT)
        raise
    _commit_and_open_pr(ROOT, current, version, committer_date=release_committer_date(day))
    return 0


def command_render_changelog(output: str, top_date: str | None) -> int:
    text = render_changelog(ROOT, top_date=top_date)
    Path(output).write_text(text, encoding="utf-8")
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


def bump(root: Path, new_version: str) -> None:
    _require_stable(new_version)
    current = _project_version(root / "pyproject.toml")
    _require_greater(current, new_version)
    updates = _collect(root, current, new_version)
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


def changelog_plan(*, version: str, tag_commit: str | None) -> ChangelogPlan:
    if tag_commit is None:
        return ChangelogPlan(tag=f"v{version}", rewrite_top_date=True, through=None)
    return ChangelogPlan(tag=None, rewrite_top_date=False, through=tag_commit)


def replace_top_heading_date(text: str, day: str) -> str:
    dated = _require_day(day)
    match = HEADING_DATE.search(text)
    if match is None:
        message = "changelog is missing a dated heading"
        raise SystemExit(message)
    return f"{text[: match.start()]}{match.group(1)} - {dated}{text[match.end() :]}"


def release_committer_date(day: str) -> str:
    return f"{_require_day(day)}{RELEASE_COMMIT_TIME}"


def render_changelog(root: Path, *, top_date: str | None = None) -> str:
    if top_date is not None:
        _require_day(top_date)
    _sync_changelog_tools()
    version = _project_version(root / "pyproject.toml")
    plan = changelog_plan(version=version, tag_commit=_local_tag_commit(root, version))
    text = _git_cliff(root, plan.tag, through=plan.through)
    if plan.rewrite_top_date:
        # a virtual --tag is dated with the clock; the tagged commit's noon UTC time is stable
        day = top_date if top_date is not None else _committer_utc_day(root)
        text = replace_top_heading_date(text, day)
    return _format_changelog(text)


def _collect(root: Path, current: str, new: str) -> dict[Path, str]:
    updates: dict[Path, str] = {
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


def _require_day(day: str) -> str:
    if DAY.fullmatch(day) is None:
        message = f"{day} is not YYYY-MM-DD"
        raise SystemExit(message)
    return day


def _committer_utc_day(root: Path) -> str:
    seconds = _git(root, "log", "-1", "--format=%ct").strip()
    try:
        instant = int(seconds)
    except ValueError:
        message = f"unexpected committer time {seconds}"
        raise SystemExit(message) from None
    return datetime.fromtimestamp(instant, UTC).date().isoformat()


def _local_tag_commit(root: Path, version: str) -> str | None:
    completed = run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [_executable("git"), "rev-parse", "-q", "--verify", f"refs/tags/v{version}^{{commit}}"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return None
    sha = completed.stdout.strip()
    if not sha:
        message = f"v{version} did not resolve"
        raise SystemExit(message)
    return sha


def _sync_changelog_tools() -> None:
    _run(
        [
            _executable("uv"),
            "sync",
            "--project",
            str(ROOT),
            "--frozen",
            "--inexact",
            "--only-group",
            "changelog",
            "--no-install-workspace",
        ],
        ROOT,
    )


def _git_cliff(root: Path, tag: str | None, *, through: str | None) -> str:
    with TemporaryDirectory() as directory:
        output = Path(directory) / "changelog.md"
        argv = [
            _executable("uv"),
            "run",
            "--project",
            str(ROOT),
            "--frozen",
            "--no-sync",
            "git-cliff",
            "--offline",
            "--config",
            str(root / "cliff.toml"),
            "-o",
            str(output),
        ]
        if tag is not None:
            argv.extend(("--tag", tag))
        elif through is not None:
            argv.append(through)
        _run(argv, root)
        return output.read_text(encoding="utf-8")


def _format_changelog(text: str) -> str:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "changelog.md"
        path.write_text(text, encoding="utf-8")
        _run(
            [
                _executable("uv"),
                "run",
                "--project",
                str(ROOT),
                "--frozen",
                "--no-sync",
                "rumdl",
                "fmt",
                str(path),
            ],
            ROOT,
        )
        return path.read_text(encoding="utf-8")


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


def _commit_and_open_pr(root: Path, old: str, new: str, *, committer_date: str) -> None:
    branch = f"release/{new}"
    message = COMMIT_MESSAGE.format(version=new)
    _git(root, "checkout", "-b", branch)
    _git(root, "add", "--", *RELEASE_PATHS)
    env = environ.copy()
    env["GIT_AUTHOR_DATE"] = committer_date
    env["GIT_COMMITTER_DATE"] = committer_date
    _run([_executable("git"), "commit", "-m", message], root, env=env)
    _git(root, "push", "-u", "origin", branch)
    _run(
        [_executable("gh"), "pr", "create", "--title", message, "--body", _pull_request_body(old, new)],
        root,
    )


def _pull_request_body(old: str, new: str) -> str:
    return (
        "## Summary\n"
        f"- Bumps every lockstep distribution from {old} to {new}.\n"
        "- `docs/changelog.md` is generated from commits.\n"
        "\n"
        f"Merging to main tags `v{new}` after CI is green. That tag runs Release, which publishes.\n"
        "\n"
        "## Test plan\n"
        "- [ ] Review the generated changelog\n"
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

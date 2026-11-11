import asyncio
import os
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
from contextlib import AsyncExitStack, ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile
from typing import TYPE_CHECKING, Final

from rowx_core.errors import DatabaseError, InterfaceError

from rowx._backend import backend_for
from rowx.check.config import Config, ConfigError, load_config
from rowx.check.migrate import load_migrator, resolve_target
from rowx.check.render import render_text, strip_controls
from rowx.check.runner import CheckError, RunResult, SiteCoverage, redact, run_check
from rowx.check.scratch import Scratch
from rowx.check.walk import discover_files

EXIT_INCOMPLETE: Final[int] = 2

if TYPE_CHECKING:
    from argparse import Namespace
    from typing import BinaryIO


@dataclass(slots=True, kw_only=True)
class Snapshot:
    sha: str
    root: Path
    config: Config
    files: list[Path]


def _git(*args: str) -> bytes:
    try:
        return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
            ["git", "--no-replace-objects", *args],  # ruff: ignore[start-process-with-partial-path]
            check=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        msg = f"could not read Git revision: {error}"
        raise CheckError(msg) from None


def _materialize(sha: str, root: Path) -> None:
    entries = _git("ls-tree", "-rz", "--full-tree", sha).split(b"\0")
    blobs: list[tuple[Path, bytes]] = []
    for entry in entries:
        if not entry:
            continue
        metadata, raw_name = entry.split(b"\t", 1)
        mode, kind, oid = metadata.split(b" ")
        name = os.fsdecode(raw_name)
        path = root / name
        if (
            not path.resolve().is_relative_to(root)
            or ".git" in path.relative_to(root).parts
            or mode not in {b"100644", b"100755"}
            or kind != b"blob"
        ):
            msg = f"unsupported path in revision {sha}: {name}"
            raise CheckError(msg)
        blobs.append((path, oid))
    with TemporaryFile() as requests, TemporaryFile() as output:
        for _, oid in blobs:
            requests.write(oid + b"\n")
        requests.seek(0)
        subprocess.run(
            ["git", "--no-replace-objects", "cat-file", "--batch"],  # ruff: ignore[start-process-with-partial-path]
            stdin=requests,
            stdout=output,
            check=True,
        )
        output.seek(0)
        directories: set[Path] = {root}
        for path, oid in blobs:
            _write_blob(output, path, oid, root, directories)


def _write_blob(output: BinaryIO, path: Path, oid: bytes, root: Path, directories: set[Path]) -> None:
    object_id, kind, size = output.readline().split(b" ")
    if object_id != oid or kind != b"blob":
        msg = "unexpected Git object"
        raise ValueError(msg)
    missing = []
    parent = path.parent
    while parent not in directories:
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            msg = f"filesystem collision: {directory.relative_to(root)}"
            raise CheckError(msg) from None
        directories.add(directory)
    try:
        target = path.open("xb")
    except FileExistsError:
        msg = f"filesystem collision: {path.relative_to(root)}"
        raise CheckError(msg) from None
    with target:
        remaining = int(size)
        while remaining:
            chunk = output.read(min(remaining, 65536))
            if not chunk:
                msg = "truncated Git object"
                raise ValueError(msg)
            target.write(chunk)
            remaining -= len(chunk)
    if output.read(1) != b"\n":
        msg = "truncated Git object"
        raise ValueError(msg)


def _snapshot(revision: str, root: Path) -> Snapshot:
    sha = _git("rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}").decode().strip()
    try:
        return _load_snapshot(sha, root)
    except (CheckError, ConfigError, OSError, ValueError, subprocess.CalledProcessError) as error:
        msg = f"incomplete: loading revision {sha}: {error}"
        raise CheckError(msg) from None


def _load_snapshot(sha: str, root: Path) -> Snapshot:
    _materialize(sha, root)
    project = root / "pyproject.toml"
    config = load_config(root, project) if project.is_file() else Config(root=root)
    if config.migrate is None:
        msg = f"revision {sha} has no [tool.rowx.migrate] configuration"
        raise CheckError(msg)
    target = resolve_target(config, None).directory.resolve()
    if not target.is_relative_to(root):
        msg = f"migration path escapes revision {sha}"
        raise CheckError(msg)
    for include in config.include:
        if not (root / include).resolve().is_relative_to(root):
            msg = f"source path escapes revision {sha}"
            raise CheckError(msg)
    files = discover_files(config, root, use_git=False)
    return Snapshot(sha=sha, root=root, config=config, files=files)


def _complete(result: RunResult, *, own: bool) -> bool:
    if own and any(not report.wider_nullability for report in result.reports):
        return False
    checked = [site for site in result.coverage if site.kind != "query_unchecked"]
    if own and not any(site.described for site in checked):
        return False
    return all(_site_complete(site, own=own) for site in checked)


def _site_complete(site: SiteCoverage, *, own: bool) -> bool:
    if site.operational_failure:
        return False
    if not site.described:
        return not own and site.sql_failed
    if site.row_unresolved or (site.row_comparison != "compared" and (own or not site.row_failed)):
        return False
    if site.parameters_aligned:
        return all(p.mismatch is not None or p.sql_type_key is None for p in site.parameters)
    return not own and site.parameters_failed


def _show(result: RunResult, snapshot: Snapshot) -> None:
    from rowx.check.cli import _coverage  # ruff: ignore[import-outside-top-level]

    reports = []
    for report in result.reports:
        if report.wider_nullability:
            continue
        diagnostic = report.diagnostic
        notes = tuple(note.replace(str(snapshot.root) + "/", "") for note in diagnostic.notes)
        reports.append(replace(report, diagnostic=replace(diagnostic, notes=notes)))
    sys.stdout.write(render_text(reports, result.sources, snapshot.root))
    sys.stdout.write(_coverage(result, snapshot.root))


async def _run(base: Snapshot, head: Snapshot, dsn: str, jobs: int, scratches: list[Scratch]) -> int:
    backend = backend_for(dsn)
    async with AsyncExitStack() as stack:
        urls: list[str] = []
        for snapshot in (base, head):
            try:
                scratch = Scratch(dsn, backend.admin(dsn), sweep_previous=False)
                scratches.append(scratch)
                migrator = load_migrator(resolve_target(snapshot.config, None), dsn, backend)
                built = await stack.enter_async_context(scratch.session(migrator))
            except (CheckError, DatabaseError, InterfaceError, OSError) as error:
                msg = f"incomplete: building schema for revision {snapshot.sha}: {error}"
                raise CheckError(msg) from None
            urls.append(built.url)
        own: list[RunResult] = []
        for snapshot, url in zip((base, head), urls, strict=True):
            sys.stdout.write(f"own schema: source {snapshot.sha}, schema {snapshot.sha}\n")
            result = await run_check(snapshot.config, snapshot.files, url, jobs, boundary=snapshot.root)
            own.append(result)
            _show(result, snapshot)
        if not all(_complete(result, own=True) for result in own):
            sys.stdout.write("incomplete: own-schema checks did not establish complete coverage\n")
            return 2
        incompatible = False
        incomplete = False
        for direction, snapshot, schema, url in (
            ("migrate first", base, head, urls[1]),
            ("deploy first", head, base, urls[0]),
        ):
            sys.stdout.write(f"{direction}: source {snapshot.sha}, schema {schema.sha}\n")
            result = await run_check(snapshot.config, snapshot.files, url, jobs, boundary=snapshot.root)
            _show(result, snapshot)
            incompatible |= any(not report.wider_nullability for report in result.reports)
            incomplete |= not _complete(result, own=False)
        if incomplete:
            sys.stdout.write("incomplete: some comparisons could not be established\n")
            return 2
        return 1 if incompatible else 0


def _handle_snapshots(args: Namespace, dsn: str, scratches: list[Scratch]) -> int:
    with ExitStack() as stack:
        base_root = Path(stack.enter_context(TemporaryDirectory(prefix="rowx-base-"))).resolve()
        head_root = Path(stack.enter_context(TemporaryDirectory(prefix="rowx-head-"))).resolve()
        base = _snapshot(args.against, base_root)
        head = _snapshot(args.head, head_root)
        status = asyncio.run(_run(base, head, dsn, args.jobs, scratches))
        for scratch in scratches:
            scratch.sweep()
        if any(scratch.leaked for scratch in scratches):
            sys.stdout.write("incomplete: scratch cleanup failed\n")
            return 2
    if status != EXIT_INCOMPLETE:
        sys.stdout.write("incompatible\n" if status == 1 else "compatible checked subset\n")
    return status


def handle(args: Namespace) -> int:
    dsn = args.database_url or os.environ.get("ROWX_DATABASE_URL", "")
    if not dsn.strip():
        msg = "pass --database-url or set ROWX_DATABASE_URL"
        raise CheckError(msg)
    scratches: list[Scratch] = []
    sys.stderr.write(
        "warning: use a disposable PostgreSQL server; trusted migrations can affect shared cluster resources\n"
    )
    try:
        return _handle_snapshots(args, dsn, scratches)
    except (CheckError, OSError, ValueError, subprocess.CalledProcessError) as error:
        raise CheckError(strip_controls(redact(str(error), dsn))) from None
    finally:
        for scratch in scratches:
            scratch.sweep()

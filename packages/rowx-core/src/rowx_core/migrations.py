from collections import defaultdict
from dataclasses import dataclass
from hashlib import sha384
from itertools import pairwise
from stat import S_ISREG
from typing import TYPE_CHECKING, Final, Literal

from rowx_core.errors import MigrationSourceError

if TYPE_CHECKING:
    from datetime import datetime, timedelta
    from pathlib import Path


type MigrationKind = Literal["simple", "reversible"]
MAX_VERSION: Final[int] = 9223372036854775807


@dataclass(frozen=True, slots=True, kw_only=True)
class Migration:
    version: int
    description: str
    kind: MigrationKind
    # the simple file, or the `.up.sql`
    path: Path
    # the `.down.sql` of a reversible migration
    down_path: Path | None
    # SHA-384 of the up file's raw bytes; the down checksum is never compared against the database
    checksum: bytes
    down_checksum: bytes | None
    no_transaction: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class MigrationResult:
    migration: Migration
    # None for a dry run or a skip
    elapsed: timedelta | None


@dataclass(frozen=True, slots=True, kw_only=True)
class MigrationStatus:
    version: int
    description: str
    state: str
    # None when the migration is missing locally
    kind: MigrationKind | None = None
    installed_on: datetime | None = None
    execution_time: timedelta | None = None
    local_checksum: bytes | None = None
    applied_checksum: bytes | None = None


def _error(message: str, path: Path, version: int | None = None) -> MigrationSourceError:
    return MigrationSourceError(message, path=path, version=version)


@dataclass(slots=True, kw_only=True, frozen=True)
class _File:
    version: int
    kind: str
    description: str
    path: Path
    data: bytes


MAX_VERSION_DIGITS: Final[int] = len(str(MAX_VERSION))
KIND_ORDER: Final[dict[str, int]] = {"simple": 0, "up": 1, "down": 2}


def _parse_name(path: Path) -> tuple[int, str, str]:
    name = path.name
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        msg = f"migration file {path} is not a valid name: the file name is not valid UTF-8"
        raise _error(msg, path) from None
    kind = "up" if name.endswith(".up.sql") else "down" if name.endswith(".down.sql") else "simple"
    suffix = f".{kind}.sql" if kind != "simple" else ".sql"
    version_text, sep, description = name.removesuffix(suffix).partition("_")
    normalized = version_text.lstrip("0") or "0"
    reason = None
    if not sep:
        reason = "expected <VERSION>_<DESCRIPTION>.sql"
    elif not version_text or not version_text.isascii() or not version_text.isdecimal():
        reason = "the version must be a non-negative integer"
    elif len(normalized) > MAX_VERSION_DIGITS or int(normalized) > MAX_VERSION:
        reason = "the version does not fit in a 64-bit integer"
    if reason is not None:
        msg = f"migration file {path} is not a valid name: {reason}"
        raise _error(msg, path)
    return int(normalized), description.replace("_", " "), kind


def _read_file(path: Path) -> _File:
    version, description, kind = _parse_name(path)
    try:
        data = path.read_bytes()
        data.decode("utf-8")
    except UnicodeDecodeError:
        msg = f"migration file {path} is not valid UTF-8"
        raise _error(msg, path) from None
    except OSError as error:
        msg = f"cannot read migration file {path}: {error}"
        raise _error(msg, path) from error
    return _File(version=version, kind=kind, description=description, path=path, data=data)


def _paths(directory: Path) -> list[Path]:
    try:
        paths = sorted(path for path in directory.iterdir() if path.name.endswith(".sql"))
    except OSError as error:
        msg = f"cannot read migration directory {directory}: {error}"
        raise _error(msg, directory) from error
    found: list[Path] = []
    for path in paths:
        try:
            regular = S_ISREG(path.stat().st_mode)
        except OSError as error:
            msg = f"cannot read migration file {path}: {error}"
            raise _error(msg, path) from error
        if regular:
            found.append(path)
    return found


def _paired(entries: list[_File]) -> tuple[_File, _File | None]:
    for first, second in pairwise(entries):
        version = first.version
        if first.kind == second.kind:
            msg = f"migration version {version} is defined twice: {first.path} and {second.path}"
            raise _error(msg, second.path, version)
        if (first.kind == "simple") != (second.kind == "simple"):
            msg = (
                f"migration version {version} mixes a simple and a reversible migration: {first.path} and {second.path}"
            )
            raise _error(msg, second.path, version)
    by_kind = {entry.kind: entry for entry in entries}
    if "simple" in by_kind:
        return by_kind["simple"], None
    for kind, other in (("up", "down"), ("down", "up")):
        if kind in by_kind and other not in by_kind:
            entry = by_kind[kind]
            msg = f"migration file {entry.path} has no matching .{other}.sql file"
            raise _error(msg, entry.path, entry.version)
    return by_kind["up"], by_kind["down"]


def discover_migrations(directory: Path) -> tuple[Migration, ...]:
    groups: dict[int, list[_File]] = defaultdict(list)
    for path in _paths(directory):
        entry = _read_file(path)
        groups[entry.version].append(entry)
    migrations: list[Migration] = []
    for version, entries in sorted(groups.items()):
        entries.sort(key=lambda entry: (KIND_ORDER[entry.kind], entry.path))
        entry, down = _paired(entries)
        migrations.append(
            Migration(
                version=version,
                description=entry.description,
                kind="simple" if down is None else "reversible",
                path=entry.path,
                down_path=None if down is None else down.path,
                checksum=sha384(entry.data).digest(),
                down_checksum=None if down is None else sha384(down.data).digest(),
                no_transaction=entry.data.startswith(b"-- no-transaction"),
            )
        )
    return tuple(migrations)

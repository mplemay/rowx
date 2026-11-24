import asyncio
import errno
import os
import secrets
import shutil
import sys
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, override

from rowx_core.backend import Provisioner
from rowx_core.errors import DatabaseError, InterfaceError

from rowx_sqlite._connection import Connection, connect
from rowx_sqlite._core import create_database, drop_database, sqlite_version
from rowx_sqlite._options import ConnectOptions
from rowx_sqlite._pool import Pool
from rowx_sqlite._scratch import SIDECARS, scratch_url, use_rollback_journal

if sys.platform == "win32":
    import msvcrt
else:
    from fcntl import LOCK_EX, LOCK_NB, flock

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from rowx_core.backend import CacheDirectory, MigrationService

CACHE_NAME: Final[str] = "rowx-sqlite"
TEST_PREFIX: Final[str] = "rowx_test_"
TMP_MARKER: Final[str] = "_tmp_"
DATABASE_SUFFIX: Final[str] = ".db"
HOLD_SUFFIX: Final[str] = ".hold"
LOCK_SUFFIX: Final[str] = ".lock"
LOCK_POLL: Final[float] = 0.05
LOCK_MODE: Final[int] = 0o600
WINDOWS_BUSY: Final[frozenset[int]] = frozenset({errno.EACCES, errno.EDEADLK})


# a thread call that a cancel (Ctrl-C) cannot leave half done: the cancel waits for it, so what the thread writes is
# there for the cleanup that follows to remove
async def finish[T](work: Awaitable[T]) -> T:
    task = asyncio.ensure_future(work)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.wait([task])
        raise


# a non-blocking exclusive lock on a file, taken the same way on POSIX and Windows; the lock lives as long as the
# descriptor, so a crashed process never leaves it held
def try_lock(path: Path) -> int | None:
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, LOCK_MODE)
    try:
        if sys.platform == "win32":
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            flock(descriptor, LOCK_EX | LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    except OSError as error:
        os.close(descriptor)
        # Windows reports a lock that is taken as an `OSError` of its own; any other failure is real
        if sys.platform == "win32" and error.errno in WINDOWS_BUSY:
            return None
        raise
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


# polled on the loop rather than blocked on in a thread, so that a cancelled wait never leaves a descriptor that a
# thread goes on to lock
async def lock(path: Path) -> int:
    # a file lock has nothing to await, so it is polled
    while (descriptor := try_lock(path)) is None:  # ruff: ignore[async-busy-wait]
        await asyncio.sleep(LOCK_POLL)
    return descriptor


def unlock(descriptor: int) -> None:
    with suppress(OSError):
        os.close(descriptor)


def scratch_directory(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def remove_files(path: Path) -> None:
    for suffix in SIDECARS:
        with suppress(OSError):
            Path(f"{path}{suffix}").unlink(missing_ok=True)


# `<p>` and the lock and prefix that belong to the project, from a template name `rowx_tpl_<p>_<hash>`
def project_base(name: str) -> str:
    return name.rpartition("_")[0]


def check_project(name: str, project: str) -> None:
    if not project_base(name).endswith(f"_{project}"):
        msg = f"the template name {name} does not belong to the project {project}"
        raise ValueError(msg)


def temporary_path(final: Path) -> Path:
    return final.with_name(f"{final.stem}{TMP_MARKER}{os.getpid()}_{secrets.token_hex(4)}{DATABASE_SUFFIX}")


def sweep_temporaries(directory: Path, base: str) -> None:
    # under the project lock every build is finished or dead, so a temporary file is a leftover
    for leftover in directory.glob(f"{base}_*{TMP_MARKER}*"):
        if leftover.name.endswith(DATABASE_SUFFIX):
            remove_files(leftover)


# drops the hold files that no process holds any more, and returns this project's stale templates that no session
# holds. A template name never contains a dot, so the text before the first dot of a hold file names its template
def find_stale(directory: Path, base: str, keep: Path) -> list[Path]:
    held: set[str] = set()
    for hold in directory.glob(f"{base}_*{HOLD_SUFFIX}"):
        if (descriptor := try_lock(hold)) is None:
            held.add(hold.name.partition(".")[0])
            continue
        unlock(descriptor)
        with suppress(OSError):
            hold.unlink()
    return [
        found
        for found in directory.glob(f"{base}_*{DATABASE_SUFFIX}")
        if found != keep and TMP_MARKER not in found.name and found.stem not in held
    ]


def take_hold(final: Path) -> tuple[Path, int]:
    path = final.with_name(f"{final.stem}.{os.getpid()}.{secrets.token_hex(4)}{HOLD_SUFFIX}")
    if (descriptor := try_lock(path)) is None:
        msg = f"could not hold the template {final.name}"
        raise InterfaceError(msg)
    return path, descriptor


def release_hold(path: Path, descriptor: int) -> None:
    # closed first: Windows cannot delete a file that is open
    unlock(descriptor)
    with suppress(OSError):
        path.unlink()


async def build(migrator: MigrationService | None, final: Path) -> None:
    temporary = temporary_path(final)

    async def materialize() -> None:
        await finish(create_database(str(temporary)))
        if migrator is not None:
            await migrator.run_on(scratch_url(temporary))
        await use_rollback_journal(temporary, "template database")
        await finish(asyncio.to_thread(os.replace, temporary, final))

    try:
        await materialize()
    except BaseException:
        await finish(asyncio.to_thread(remove_files, temporary))
        raise


async def drop_stale(stale: list[Path]) -> None:
    for found in stale:
        # a stale template that cannot be removed is only a leftover
        with suppress(DatabaseError, InterfaceError, OSError):
            await finish(drop_database(str(found)))


# `finish` alone would drop the hold that the thread took while the cancel waited for it, and nothing else knows it
async def claim_hold(final: Path) -> tuple[Path, int]:
    taking = asyncio.ensure_future(asyncio.to_thread(take_hold, final))
    try:
        return await asyncio.shield(taking)
    except asyncio.CancelledError:
        await asyncio.wait([taking])
        if not taking.cancelled() and taking.exception() is None:
            release_hold(*taking.result())
        raise


async def claim_template(
    migrator: MigrationService | None, directory: Path, final: Path, *, keep_stale: bool
) -> tuple[Path, int]:
    base = project_base(final.stem)
    await finish(asyncio.to_thread(sweep_temporaries, directory, base))
    stale = await finish(asyncio.to_thread(find_stale, directory, base, final))
    if not await finish(asyncio.to_thread(final.exists)):
        await build(migrator, final)
    # taken under the project lock, so no cleanup can drop the template between its lookup and the claim
    hold = await claim_hold(final)
    try:
        if not keep_stale:
            await drop_stale(stale)
    except BaseException:
        release_hold(*hold)
        raise
    return hold


async def locked_template(
    migrator: MigrationService | None, directory: Path, final: Path, *, keep_stale: bool
) -> tuple[Path, int]:
    descriptor = await lock(directory / f"{project_base(final.stem)}{LOCK_SUFFIX}")
    try:
        return await claim_template(migrator, directory, final, keep_stale=keep_stale)
    finally:
        unlock(descriptor)


@dataclass(slots=True, kw_only=True, frozen=True)
class SqliteHold:
    release: Callable[[], Awaitable[None]]

    async def close(self) -> None:
        await self.release()


class SqliteProvisioner(Provisioner):
    def __init__(self, url: str, *, cache: CacheDirectory | None = None) -> None:
        # the URL only selects the backend: the file it names is never opened, created or modified
        ConnectOptions.from_dsn(url)
        self._cache = cache
        self._identity = (f"sqlite:{sqlite_version()}",)
        self._template: tuple[str, Path] | None = None
        self._clones: Path | None = None
        # the files that tests own, by the logical name `clone` returned
        self._live: dict[str, Path] = {}

    @property
    @override
    def identity(self) -> tuple[str, ...]:
        return self._identity

    @override
    async def ensure_template(
        self, migrator: MigrationService | None, name: str, *, keep_stale: bool, project: str
    ) -> SqliteHold:
        check_project(name, project)
        scratch: Path | None = None
        if self._cache is not None:
            directory = await asyncio.to_thread(self._cache, CACHE_NAME)
        else:
            # without a cache the template lives, and is built once, in a directory of this session
            scratch = directory = await asyncio.to_thread(scratch_directory, "rowx-templates-")
        final = directory / f"{name}{DATABASE_SUFFIX}"
        try:
            hold = await locked_template(migrator, directory, final, keep_stale=keep_stale)
        except BaseException:
            if scratch is not None:
                await asyncio.to_thread(shutil.rmtree, scratch, ignore_errors=True)
            raise
        self._template = (name, final)
        path, descriptor = hold

        async def release() -> None:
            await self._release()
            await asyncio.to_thread(release_hold, path, descriptor)
            if scratch is not None:
                await asyncio.to_thread(shutil.rmtree, scratch, ignore_errors=True)

        return SqliteHold(release=release)

    async def _release(self) -> None:
        self._template = None
        live, self._live = list(self._live.values()), {}
        clones, self._clones = self._clones, None
        for path in live:
            await asyncio.to_thread(remove_files, path)
        if clones is not None:
            await asyncio.to_thread(shutil.rmtree, clones, ignore_errors=True)

    @override
    async def clone(self, template: str, project: str) -> str:
        if self._template is None or self._template[0] != template:
            msg = "the SQLite template is not built"
            raise InterfaceError(msg)
        if self._clones is None:
            self._clones = await asyncio.to_thread(scratch_directory, "rowx-clones-")
        handle = f"{TEST_PREFIX}{project}_{secrets.token_hex(8)}"
        path = self._clones / f"{handle}{DATABASE_SUFFIX}"
        # the backup API copies the pages, so a template that a test never writes to stays as it was built
        await create_database(str(path), template=str(self._template[1]))
        self._live[handle] = path
        return handle

    @override
    async def drop(self, name: str) -> None:
        if (path := self._live.get(name)) is not None:
            await drop_database(str(path))
            del self._live[name]

    @override
    def with_database(self, name: str) -> str:
        if (path := self._live.get(name)) is None:
            msg = "the SQLite test database does not exist (it was dropped, or never cloned)"
            raise InterfaceError(msg)
        return scratch_url(path)

    @override
    async def connect(self, dsn: str) -> Connection:
        return await connect(dsn)

    @override
    def pool(self, dsn: str) -> Pool:
        return Pool(dsn)


def provisioner(dsn: str, /, *, cache: CacheDirectory | None = None) -> Provisioner:
    return SqliteProvisioner(dsn, cache=cache)

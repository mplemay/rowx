import asyncio
import hashlib
import os
import secrets
import shutil
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Final, override

from rowx_core.backend import Provisioner
from rowx_core.errors import InterfaceError

from rowx_postgres._connection import Connection, connect
from rowx_postgres._embedded import (
    EmbeddedDatabase,
    SnapshotRejectedError,
    is_embedded_url,
    load_engine,
    parse_embedded_url,
    start_from_snapshot,
    start_owned,
    take_snapshot,
)
from rowx_postgres._pool import Pool
from rowx_postgres._scratch import socket_url
from rowx_postgres._services import PostgresProvisioner

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from rowx_core.backend import CacheDirectory, MigrationService

CACHE_NAME: Final[str] = "rowx-embedded"
COMPANION_DISTRIBUTION: Final[str] = "rowx-embedded"
SNAPSHOT_SUFFIX: Final[str] = ".snapshot"
TEMP_SUFFIX: Final[str] = ".tmp"
LOCK_SUFFIX: Final[str] = ".lock"
TEST_PREFIX: Final[str] = "rowx_test_"
# how long a test that holds `rowx_connection` waits on `rowx_pool` before the 53300 hint surfaces. The slot is refused at
# once while a session is active, so the pool retries until this runs out. It is longer than the engine's own wait for a
# previous session's teardown (10 s), so a slow teardown on a loaded machine ends in the engine's answer rather than a
# `PoolTimeoutError`. It is only the wait for the single slot, so a pooled query that queues behind a statement of the
# same test for longer than this fails with `PoolTimeoutError`; a test that needs more builds its own `Pool`
POOL_ACQUIRE_TIMEOUT: Final[float] = 15.0
POOL_MAX_SIZE: Final[int] = 1
DIGEST_SIZE: Final[int] = hashlib.sha256().digest_size
LOCK_POLL: Final[float] = 0.05


# everything a snapshot is only valid for: the companion, the engine, the runtime and module that booted it, the
# container layout and the extensions in it
def identity() -> tuple[str, ...]:
    engine = load_engine()
    try:
        companion = version(COMPANION_DISTRIBUTION)
    except PackageNotFoundError:
        companion = "unknown"
    return (
        f"rowx-embedded:{companion}",
        f"engine:{engine.ENGINE_VERSION}",
        f"runtime:{engine.RUNTIME_VERSION}",
        f"wasm:{engine.WASM_SHA256}",
        f"snapshot-format:{engine.SNAPSHOT_FORMAT}",
        f"extensions:{','.join(sorted(engine.EXTENSIONS))}",
    )


# a thread call that a cancel (Ctrl-C) cannot leave half done: the cancel waits for it, so what the thread writes is
# there for the cleanup that follows to remove
async def finish[T](work: Awaitable[T]) -> T:
    task = asyncio.ensure_future(work)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.wait([task])
        raise


def scratch_directory() -> Path:
    return Path(tempfile.mkdtemp(prefix="rowx-snapshots-"))


def snapshot_path(directory: Path, project: str, name: str) -> Path:
    # a template name ends in its hash (hex, so no underscore): `<prefix><p>_<h>`
    return directory / f"{project}_{name.rpartition('_')[2]}{SNAPSHOT_SUFFIX}"


# a non-blocking attempt at the one lock per project (never deleted), so that a build and a cleanup of the project's
# files never overlap. `fcntl` is POSIX-only and the embedded engine ships for POSIX platforms only
def try_lock(path: Path) -> int | None:
    from fcntl import LOCK_EX, LOCK_NB, flock  # ruff: ignore[import-outside-top-level]

    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        flock(descriptor, LOCK_EX | LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


# polled on the loop rather than blocked on in a thread, so that a cancelled wait never leaves a descriptor that a
# thread goes on to lock
async def lock(path: Path) -> int:
    # `flock` has nothing to await, so the lock is polled
    while (descriptor := try_lock(path)) is None:  # ruff: ignore[async-busy-wait]
        await asyncio.sleep(LOCK_POLL)
    return descriptor


def unlock(descriptor: int) -> None:
    # closing the descriptor releases the lock, so a crashed process never leaves it held
    with suppress(OSError):
        os.close(descriptor)


# a snapshot file is the snapshot followed by its SHA-256, so that one rename publishes both
def read_verified(path: Path) -> bytes | None:
    try:
        blob = path.read_bytes()
    except FileNotFoundError:
        return None
    if len(blob) <= DIGEST_SIZE:
        return None
    view = memoryview(blob)
    body, digest = view[:-DIGEST_SIZE], view[-DIGEST_SIZE:]
    if hashlib.sha256(body).digest() != digest:
        return None
    return bytes(body)


def write_sealed(path: Path, data: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(data)
        handle.write(hashlib.sha256(data).digest())
        handle.flush()
        os.fsync(handle.fileno())


# written beside the final name and renamed into it, so a reader sees a whole snapshot or none
def publish_blocking(path: Path, project: str, data: bytes) -> None:
    for leftover in path.parent.glob(f"{project}_*{TEMP_SUFFIX}"):
        # under the project lock, any temporary file is from a build that died
        with suppress(OSError):
            leftover.unlink()
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}{TEMP_SUFFIX}")
    try:
        write_sealed(temporary, data)
        temporary.replace(path)
    except BaseException:
        with suppress(OSError):
            temporary.unlink()
        raise


# this project's other snapshots only: another project's `<p>` never matches
def remove_stale_blocking(keep: Path, project: str) -> None:
    for stale in keep.parent.glob(f"{project}_*{SNAPSHOT_SUFFIX}"):
        if stale != keep:
            with suppress(OSError):
                stale.unlink()


async def build_snapshot(migrator: MigrationService | None, query: str) -> bytes:
    database = await start_owned(None)
    try:
        if migrator is not None:
            await migrator.run_on(socket_url(database.dsn, query))
        return await take_snapshot(database)
    finally:
        # nothing of a build outlives it, and a statement that is still running must not hold it up
        database.abandon()


@dataclass(slots=True, kw_only=True, frozen=True)
class SnapshotHold:
    release: Callable[[], Awaitable[None]]

    async def close(self) -> None:
        await self.release()


@dataclass(slots=True, kw_only=True, frozen=True)
class Template:
    directory: Path
    migrator: MigrationService | None
    name: str
    keep_stale: bool
    project: str


class EmbeddedProvisioner(Provisioner):
    def __init__(self, url: str, *, cache: CacheDirectory | None = None) -> None:
        target, self._query = parse_embedded_url(url)
        if target.directory is not None:
            msg = (
                "rowx.testing boots a fresh database from a snapshot for every test; "
                "use embedded:// (a directory is one persistent database)"
            )
            raise ValueError(msg)
        self._cache = cache
        # loads the engine now, so a missing extra fails before anything is built. The query is forwarded to the
        # migration run, so it shapes the snapshot too
        self._identity = (*identity(), *((f"query:{self._query}",) if self._query else ()))
        self._snapshot: bytes | None = None
        # until one boot has worked, a snapshot the engine rejects is rebuilt once
        self._booted = False
        self._rebuild: Callable[[], Awaitable[bytes]] | None = None
        # the instances that tests hold, by the handle `clone` returned
        self._live: dict[str, EmbeddedDatabase] = {}

    @property
    @override
    def identity(self) -> tuple[str, ...]:
        return self._identity

    @override
    async def ensure_template(
        self, migrator: MigrationService | None, name: str, *, keep_stale: bool, project: str
    ) -> SnapshotHold:
        scratch: Path | None = None
        if self._cache is not None:
            directory = await asyncio.to_thread(self._cache, CACHE_NAME)
        else:
            # without a cache the snapshot lives, and is built once, in a directory of this session. A SIGKILL leaves
            # that directory behind (nothing runs at exit), as it does the engine's own temporary roots
            scratch = directory = scratch_directory()

        template = Template(directory=directory, migrator=migrator, name=name, keep_stale=keep_stale, project=project)

        async def rebuild() -> bytes:
            return await self._provide(template, reuse=False)

        try:
            self._snapshot = await self._provide(template, reuse=True)
        except BaseException:
            await self._release(scratch)
            raise
        self._rebuild = rebuild

        async def release() -> None:
            await self._release(scratch)

        return SnapshotHold(release=release)

    async def _provide(self, template: Template, *, reuse: bool) -> bytes:
        directory, project = template.directory, template.project
        path = snapshot_path(directory, project, template.name)
        descriptor = await lock(directory / f"{project}{LOCK_SUFFIX}")
        try:
            # a missing or damaged file is rebuilt; a snapshot that only the engine can refuse is handled at the boot
            if reuse and (data := await asyncio.to_thread(read_verified, path)) is not None:
                return data
            data = await build_snapshot(template.migrator, self._query)
            await finish(asyncio.to_thread(publish_blocking, path, project, data))
            if not template.keep_stale:
                await finish(asyncio.to_thread(remove_stale_blocking, path, project))
        finally:
            unlock(descriptor)
        return data

    async def _release(self, scratch: Path | None) -> None:
        self._snapshot = None
        self._rebuild = None
        # a memory instance holds nothing worth waiting for, and a statement that a test leaked running must not hold
        # the session up
        live, self._live = list(self._live.values()), {}
        for database in live:
            database.abandon()
        if scratch is not None:
            await asyncio.to_thread(shutil.rmtree, scratch, ignore_errors=True)

    # the handle names the instance a test owns until `drop`; it is never a database name
    @override
    async def clone(self, template: str, project: str) -> str:
        if self._snapshot is None:
            msg = "the embedded template is not built"
            raise InterfaceError(msg)
        try:
            database = await start_from_snapshot(self._snapshot)
        except SnapshotRejectedError:
            # a cached snapshot the engine refuses is rebuilt once; a second refusal (or any other failure) is real
            if self._booted or self._rebuild is None:
                raise
            self._snapshot = await self._rebuild()
            database = await start_from_snapshot(self._snapshot)
        self._booted = True
        handle = f"{TEST_PREFIX}{project}_{secrets.token_hex(8)}"
        self._live[handle] = database
        return handle

    @override
    async def drop(self, name: str) -> None:
        if (database := self._live.pop(name, None)) is not None:
            database.abandon()

    @override
    def with_database(self, name: str) -> str:
        return socket_url(self._live[name].dsn, self._query)

    @override
    async def connect(self, dsn: str) -> Connection:
        return await connect(dsn)

    @override
    def pool(self, dsn: str) -> Pool:
        return Pool(dsn, max_size=POOL_MAX_SIZE, acquire_timeout=POOL_ACQUIRE_TIMEOUT)


def provisioner(dsn: str, /, *, cache: CacheDirectory | None = None) -> Provisioner:
    if is_embedded_url(dsn):
        return EmbeddedProvisioner(dsn, cache=cache)
    return PostgresProvisioner(dsn)

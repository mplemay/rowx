import asyncio
import os
import platform
import posixpath
import sys
from concurrent.futures import Future
from dataclasses import dataclass
from functools import cache
from importlib import import_module
from threading import Lock, Thread
from typing import TYPE_CHECKING, Final, Protocol, Self, cast
from urllib.parse import parse_qsl, unquote, urlsplit

from rowx_core.backend import EmbeddedTargetError
from rowx_core.errors import InterfaceError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from os import PathLike
    from types import TracebackType

    from rowx_postgres._options import ConnectOptions

EMBEDDED_SCHEME: Final[str] = "embedded"
ENGINE_MODULE: Final[str] = "rowx_embedded"
# the `rowx_embedded.API_VERSION` this adapter was written against
EXPECTED_API_VERSION: Final[int] = 1
REQUIRE_ENV: Final[str] = "ROWX_REQUIRE_EMBEDDED"
# the engine ships wheels for these platforms only (the `embedded` extra carries the same marker)
SUPPORTED_PLATFORMS: Final[frozenset[tuple[str, str]]] = frozenset(
    {("darwin", "arm64"), ("linux", "x86_64"), ("linux", "aarch64")},
)
# parts of the socket DSN that the engine owns; none of them may be set from an embedded URL
RESERVED_PARAMETERS: Final[frozenset[str]] = frozenset({"host", "hostaddr", "port", "user", "password", "sslmode"})
INSTALL_HELP: Final[str] = 'help: uv add "rowx[embedded]"'


class EngineInstance(Protocol):
    @property
    def dsn(self) -> str: ...

    def snapshot(self) -> bytes: ...

    def close(self) -> None: ...

    def abandon(self, *, force: bool = False) -> None: ...


class EngineInstanceType(Protocol):
    def start(self, directory: str | None = None, snapshot: bytes | None = None) -> EngineInstance: ...


# the surface of `rowx_embedded` that this module uses, so that `ty` passes without the companion installed
class Engine(Protocol):
    API_VERSION: int
    ENGINE_VERSION: str
    RUNTIME_VERSION: str
    WASM_SHA256: str
    SNAPSHOT_FORMAT: int
    EXTENSIONS: tuple[str, ...]
    Instance: EngineInstanceType
    EngineError: type[Exception]
    SnapshotError: type[Exception]


# the engine refused a snapshot it was asked to boot (corrupt, or built by another engine): the only start failure that
# a cached snapshot can be rebuilt over
class SnapshotRejectedError(InterfaceError):
    pass


@dataclass(slots=True, kw_only=True, frozen=True)
class EmbeddedTarget:
    # `None` is a fresh in-memory database; otherwise an absolute directory with one owner at a time
    directory: str | None = None


@dataclass(slots=True, kw_only=True, frozen=True)
class LoadedEngine:
    engine: Engine
    pid: int


def is_embedded_url(dsn: str) -> bool:
    return dsn.strip().lower().startswith(f"{EMBEDDED_SCHEME}:")


# an absolute path with `.`, `..`, doubled and trailing slashes collapsed; the root itself is never a database
def check_directory(directory: str) -> str:
    if not directory.startswith("/"):
        msg = "an embedded database directory must be an absolute path"
        raise EmbeddedTargetError(msg)
    # normpath keeps exactly two leading slashes, which would name a different path on some systems
    normal = "/" + posixpath.normpath(directory).lstrip("/")
    if normal == "/":
        msg = "an embedded database directory cannot be the root"
        raise EmbeddedTargetError(msg)
    return normal


def reject_embedded(target: str | ConnectOptions, /) -> None:
    embedded = is_embedded_url(target) if isinstance(target, str) else target.embedded is not None
    if embedded:
        msg = (
            "embedded URLs are not supported here; use a server URL (embedded:// works with connect, Pool and embedded)"
        )
        raise InterfaceError(msg)


# returns the target and the query string, which is forwarded to the socket DSN
def parse_embedded_url(url: str) -> tuple[EmbeddedTarget, str]:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        # the parser's own message can quote the host part
        msg = "an embedded URL could not be parsed"
        raise EmbeddedTargetError(msg) from None
    if parts.scheme != EMBEDDED_SCHEME:
        msg = f"expected an {EMBEDDED_SCHEME}:// URL"
        raise EmbeddedTargetError(msg)
    if parts.netloc:
        msg = "an embedded URL has no host, port, user or password; use embedded:// or embedded:///absolute/path"
        raise EmbeddedTargetError(msg)
    if parts.fragment:
        msg = "an embedded URL has no fragment"
        raise EmbeddedTargetError(msg)
    seen: set[str] = set()
    for key, _ in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in RESERVED_PARAMETERS:
            msg = f"{key} cannot be set on an embedded URL"
            raise EmbeddedTargetError(msg)
        if key in seen:
            msg = f"{key} is given more than once in an embedded URL"
            raise EmbeddedTargetError(msg)
        seen.add(key)
    path = unquote(parts.path)
    return EmbeddedTarget(directory=check_directory(path) if path else None), parts.query


def current_platform() -> tuple[str, str]:
    return sys.platform, platform.machine().lower()


def missing_engine_error() -> InterfaceError:
    if current_platform() not in SUPPORTED_PLATFORMS:
        msg = "embedded Postgres is not available on this platform"
    else:
        msg = f"embedded Postgres needs the {ENGINE_MODULE} package, which is not installed\n{INSTALL_HELP}"
    return InterfaceError(msg)


@cache
def load_cached() -> LoadedEngine:
    try:
        module = import_module(ENGINE_MODULE)
    except ModuleNotFoundError as error:
        if error.name != ENGINE_MODULE:
            raise
        raise missing_engine_error() from error
    except ImportError as error:
        msg = f"{ENGINE_MODULE} could not be loaded: {error}"
        raise InterfaceError(msg) from error
    engine = cast("Engine", module)
    found = engine.API_VERSION
    if found != EXPECTED_API_VERSION:
        msg = (
            f"{ENGINE_MODULE} has API version {found}, but this rowx_postgres needs {EXPECTED_API_VERSION}\n"
            'help: uv add --upgrade "rowx[embedded]"'
        )
        raise InterfaceError(msg)
    return LoadedEngine(engine=engine, pid=os.getpid())


def load_engine() -> Engine:
    loaded = load_cached()
    # the engine threads exist only in the process that loaded it
    if loaded.pid != os.getpid():
        msg = "embedded Postgres was loaded in another process; start it after the fork"
        raise InterfaceError(msg)
    return loaded.engine


# runs `work` in a daemon thread of its own rather than the loop's executor, whose shutdown waits for every thread: an
# interrupted run must not wait for an engine call that cannot be cancelled
def in_thread[T](work: Callable[[], T]) -> Awaitable[T]:
    future: Future[T] = Future()

    def run() -> None:
        try:
            future.set_result(work())
        except BaseException as error:  # ruff: ignore[blind-except]
            future.set_exception(error)

    Thread(target=run, daemon=True).start()
    return asyncio.wrap_future(future)


class EmbeddedDatabase:
    def __init__(self, instance: EngineInstance, /, *, directory: str | None) -> None:
        self._instance = instance
        self._directory = directory
        self._pid = os.getpid()
        self._closed = False
        # a concurrent second close waits here until the first has finished
        self._close_lock = Lock()

    @property
    def dsn(self) -> str:
        self._check_use()
        return self._instance.dsn

    @property
    def directory(self) -> str | None:
        return self._directory

    @property
    def closed(self) -> bool:
        return self._closed

    def _check_use(self) -> None:
        if os.getpid() != self._pid:
            msg = "the embedded database belongs to another process; start it after the fork"
            raise InterfaceError(msg)
        if self._closed:
            msg = "the embedded database is closed"
            raise InterfaceError(msg)

    def close_blocking(self) -> None:
        # a forked child never closes (and so never deletes) the parent's instance
        if os.getpid() != self._pid:
            return
        with self._close_lock:
            if self._closed:
                return
            # only a close that succeeded counts, so a failed one can be retried
            self._instance.close()
            self._closed = True

    def snapshot_blocking(self) -> bytes:
        self._check_use()
        return self._instance.snapshot()

    # stops the instance at once without waiting for the engine thread, which a running statement can hold for as long
    # as it takes; for an interrupted run that wants nothing of a memory database. A directory is refused unless
    # `force`, because skipping the clean shutdown leaves it as a crash would
    def abandon(self, *, force: bool = False) -> None:
        if os.getpid() != self._pid or self._closed:
            return
        if self._directory is not None and not force:
            msg = "a persistent embedded database cannot be abandoned; close it"
            raise InterfaceError(msg)
        self._instance.abandon(force=force)
        self._closed = True

    # the engine thread is joined off the loop, and a cancelled caller still lets the close finish
    async def close(self) -> None:
        abandon_if_cancelled(self)
        await asyncio.shield(in_thread(self.close_blocking))

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()


# a close that runs because its task was cancelled (Ctrl-C) abandons a memory database: it holds nothing worth waiting
# for a running statement to end, which the engine cannot cancel. A directory is always closed properly
def abandon_if_cancelled(database: EmbeddedDatabase) -> None:
    if database.directory is None and (task := asyncio.current_task()) is not None and task.cancelling():
        database.abandon()


def start_blocking(directory: str | None, snapshot: bytes | None = None) -> EmbeddedDatabase:
    engine = load_engine()
    try:
        instance = engine.Instance.start(directory, snapshot)
    except engine.EngineError as error:
        # only a start from a snapshot can have one rejected
        if snapshot is not None and isinstance(error, engine.SnapshotError):
            raise SnapshotRejectedError(str(error)) from error
        raise InterfaceError(str(error)) from error
    except Exception as error:
        msg = f"the embedded engine failed to start: {error}"
        raise InterfaceError(msg) from error
    return EmbeddedDatabase(instance, directory=directory)


def close_orphan(future: Future[EmbeddedDatabase]) -> None:
    if future.cancelled() or future.exception() is not None:
        return
    database = future.result()
    # nothing owns the instance any more: a memory database is dropped on the spot, a directory is closed off the loop
    if database.directory is None:
        database.abandon()
        return
    Thread(target=database.close_blocking).start()


# the engine can't be interrupted, so a cancelled start finishes in its own thread (a plain future, which neither the
# caller's cancellation nor the loop's shutdown can cancel) and the instance nobody owns is closed there
async def start_owned(directory: str | None, snapshot: bytes | None = None) -> EmbeddedDatabase:
    future: Future[EmbeddedDatabase] = Future()

    def work() -> None:
        try:
            future.set_result(start_blocking(directory, snapshot))
        except BaseException as error:  # ruff: ignore[blind-except]
            future.set_exception(error)

    Thread(target=work).start()
    wrapped = asyncio.wrap_future(future)
    try:
        return await asyncio.shield(wrapped)
    except asyncio.CancelledError:
        future.add_done_callback(close_orphan)
        # a start that fails after the cancel has no caller to report to
        wrapped.add_done_callback(lambda done: done.cancelled() or done.exception())
        raise


async def start(*, directory: str | PathLike[str] | None = None) -> EmbeddedDatabase:
    return await start_owned(None if directory is None else check_directory(os.fspath(directory)))


# private to the checker and the pytest plugin: a snapshot is not a public API in v1
async def start_from_snapshot(snapshot: bytes, *, directory: str | None = None) -> EmbeddedDatabase:
    return await start_owned(None if directory is None else check_directory(directory), snapshot)


async def take_snapshot(database: EmbeddedDatabase, /) -> bytes:
    return await in_thread(database.snapshot_blocking)

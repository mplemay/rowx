import asyncio
import os
import posixpath
from concurrent.futures import Future
from dataclasses import dataclass, replace
from threading import Lock, Thread
from typing import TYPE_CHECKING, Final, Self
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from rowx_core.backend import EmbeddedTargetError
from rowx_core.errors import InterfaceError
from rowx_postgres._core import parse_dsn  # ruff: ignore[import-private-name]

from rowx_embedded._engine import EngineError, Instance, SnapshotError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from types import TracebackType

    from rowx_postgres import ConnectOptions

EMBEDDED_SCHEME: Final[str] = "embedded"
REQUIRE_ENV: Final[str] = "ROWX_REQUIRE_EMBEDDED"
# parts of the socket DSN that the engine owns; none of them may be set from an embedded URL or from `ConnectOptions`
RESERVED_PARAMETERS: Final[frozenset[str]] = frozenset({"host", "hostaddr", "port", "user", "password", "sslmode"})


# the engine refused a snapshot it was asked to boot (corrupt, or built by another engine): the only start failure that
# a cached snapshot can be rebuilt over
class SnapshotRejectedError(InterfaceError):
    pass


@dataclass(slots=True, kw_only=True, frozen=True)
class EmbeddedTarget:
    # `None` is a fresh in-memory database; otherwise an absolute directory with one owner at a time
    directory: str | None = None


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


# the instance's socket DSN with the other parameters of the embedded URL (`options=-c search_path=x`) added to it
def socket_url(dsn: str, query: str) -> str:
    if not query:
        return dsn
    parts = urlsplit(dsn)
    merged = urlencode([*parse_qsl(parts.query, keep_blank_values=True), *parse_qsl(query, keep_blank_values=True)])
    return urlunsplit(parts._replace(query=merged))


# the options pointed at the instance's socket DSN; Rust never sees an embedded target
def with_socket(options: ConnectOptions, dsn: str, /) -> ConnectOptions:
    socket = parse_dsn(dsn)
    return replace(
        options,
        host=socket["host"],
        port=socket["port"],
        user=socket["user"],
        sslmode=socket["sslmode"],
        database=options.database if options.database is not None else socket["database"],
    )


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
    def __init__(self, instance: Instance, /, *, directory: str | None) -> None:
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
    try:
        instance = Instance.start(directory, snapshot)
    except EngineError as error:
        # only a start from a snapshot can have one rejected
        if snapshot is not None and isinstance(error, SnapshotError):
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


# private to the checker and the pytest plugin: a snapshot is not a public API in v1
async def start_from_snapshot(snapshot: bytes, *, directory: str | None = None) -> EmbeddedDatabase:
    return await start_owned(None if directory is None else check_directory(directory), snapshot)


async def take_snapshot(database: EmbeddedDatabase, /) -> bytes:
    return await in_thread(database.snapshot_blocking)

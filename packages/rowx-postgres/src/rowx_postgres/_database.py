import asyncio
import os
from concurrent.futures import Future
from contextlib import suppress
from dataclasses import replace
from threading import Lock, Thread
from typing import TYPE_CHECKING, Final, override

from rowx_core.errors import InterfaceError, PoolTimeoutError

from rowx_postgres._connection import RESET_QUERY
from rowx_postgres._core import Pool as PoolImpl, parse_dsn
from rowx_postgres._embedded import (
    EmbeddedDatabase,
    abandon_if_cancelled,
    check_directory,
    start_blocking,
)
from rowx_postgres._options import ConnectOptions
from rowx_postgres._pool import (
    DEFAULT_ACQUIRE_TIMEOUT,
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_MAX_LIFETIME,
    DEFAULT_PING_AFTER,
    Hook,
    Pool as BasePool,
)

if TYPE_CHECKING:
    from os import PathLike

# an embedded instance serves one session at a time
MAX_SIZE: Final[int] = 1
# parts of the socket DSN that the engine owns
RESERVED_OPTIONS: Final[tuple[str, ...]] = ("host", "port", "user", "password", "sslmode")


def check_directory_argument(directory: str | PathLike[str] | None, /) -> str | None:
    return None if directory is None else check_directory(os.fspath(directory))


def check_options(options: ConnectOptions | None, /) -> ConnectOptions:
    resolved = ConnectOptions() if options is None else options
    for name in RESERVED_OPTIONS:
        if getattr(resolved, name) is not None:
            msg = f"{name} cannot be set for an embedded database"
            raise ValueError(msg)
    return resolved


# the same options pointed at the instance's socket DSN; Rust never sees an embedded target
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


class Database(BasePool):
    def __init__(  # ruff: ignore[too-many-arguments]
        self,
        directory: str | PathLike[str] | None = None,
        /,
        *,
        options: ConnectOptions | None = None,
        max_size: int = MAX_SIZE,
        acquire_timeout: float = DEFAULT_ACQUIRE_TIMEOUT,
        idle_timeout: float | None = DEFAULT_IDLE_TIMEOUT,
        max_lifetime: float | None = DEFAULT_MAX_LIFETIME,
        max_queries: int | None = None,
        ping_after: float | None = DEFAULT_PING_AFTER,
        init: Hook | None = None,
        setup: Hook | None = None,
        reset: Hook | None = None,
        reset_query: str | None = RESET_QUERY,
    ) -> None:
        if max_size != MAX_SIZE:
            msg = "an embedded database serves one session at a time; min_size and max_size cannot exceed 1"
            raise ValueError(msg)
        self._target = check_directory_argument(directory)
        resolved = check_options(options)
        super().__init__(
            resolved,
            min_size=0,
            max_size=MAX_SIZE,
            acquire_timeout=acquire_timeout,
            idle_timeout=idle_timeout,
            max_lifetime=max_lifetime,
            max_queries=max_queries,
            ping_after=ping_after,
            init=init,
            setup=setup,
            reset=reset,
            reset_query=reset_query,
        )
        self._resolved = resolved
        self._pid = os.getpid()
        # the instance starts, and the native pool is built, on first use (so the constructor does no IO), in one thread
        # whose future every first-use caller shares. The lock guards only this small state and is never held across the
        # start, and none of it is bound to an event loop.
        self._state_lock = Lock()
        self._start: Future[PoolImpl] | None = None
        self._database: EmbeddedDatabase | None = None
        self._terminated = False
        self._via_python = True

    @property
    def directory(self) -> str | None:
        return self._target

    @property
    def ready(self) -> bool:
        return self._inner is not None and not self._terminated

    @property
    def dsn(self) -> str:
        self._check_process()
        with self._state_lock:
            terminated, database = self._terminated, self._database
        if terminated:
            msg = "the pool is closed"
            raise InterfaceError(msg)
        if database is None:
            msg = "the embedded database has not started; call open() first"
            raise InterfaceError(msg)
        return database.dsn

    async def wait_ready(self) -> None:
        await self.open()

    def _check_process(self) -> None:
        if os.getpid() != self._pid:
            msg = "the pool was used in a forked process; create the pool after fork"
            raise InterfaceError(msg)

    @override
    def _native(self, options: ConnectOptions, /) -> PoolImpl | None:
        return None

    @override
    async def _started(self, limit: float | None = None) -> PoolImpl:
        self._check_process()
        if (inner := self._inner) is not None:
            return inner
        wrapped = asyncio.wrap_future(self._begin_start())
        # a start that fails with nobody waiting any more has no one to report to
        wrapped.add_done_callback(lambda done: done.cancelled() or done.exception())
        try:
            return await asyncio.wait_for(asyncio.shield(wrapped), limit)
        except TimeoutError as error:
            # the start goes on in its thread, and the pool owns what it produces
            msg = "timed out waiting for the embedded database to start"
            raise PoolTimeoutError(msg) from error

    # another thread can set `_terminated` at any moment, so every read goes through here
    def _is_stopped(self) -> bool:
        return self._terminated

    def _begin_start(self) -> Future[PoolImpl]:
        with self._state_lock:
            if self._terminated:
                msg = "the pool is closed"
                raise InterfaceError(msg)
            if self._start is None:
                self._start = Future()
                Thread(target=self._run_start, args=(self._start,)).start()
            return self._start

    def _run_start(self, future: Future[PoolImpl]) -> None:
        try:
            inner = self._start_instance()
        except BaseException as error:  # ruff: ignore[blind-except]
            # a failed start leaves nothing behind, and the next use tries again
            with self._state_lock:
                self._start = None
            future.set_exception(error)
            return
        future.set_result(inner)

    def _start_instance(self) -> PoolImpl:
        if self._is_stopped():
            msg = "the pool is closed"
            raise InterfaceError(msg)
        database = start_blocking(self._target)
        try:
            inner = self._build(with_socket(self._resolved, database.dsn))
        except BaseException:
            database.close_blocking()
            raise
        with self._state_lock:
            # whichever of this thread and close() sees the other's write owns the teardown, never both
            if not self._is_stopped():
                self._database = database
                self._inner = inner
                return inner
        inner.terminate()
        database.close_blocking()
        msg = "the pool is closed"
        raise InterfaceError(msg)

    @override
    async def close(self, *, timeout: float | None = None) -> None:
        with self._state_lock:
            self._terminated = True
            start, inner, database = self._start, self._inner, self._database
        if inner is None:
            if start is not None and not start.done():
                # the start tears down what it built when it sees the close, so wait for that
                with suppress(Exception):
                    await asyncio.shield(asyncio.wrap_future(start))
            return
        if database is not None:
            abandon_if_cancelled(database)
        try:
            await inner.close(timeout)
        finally:
            if database is not None:
                await database.close()

    @override
    def terminate(self) -> None:
        with self._state_lock:
            self._terminated = True
            inner, database = self._inner, self._database
        if inner is not None:
            inner.terminate()
        if database is not None:
            database.close_blocking()
        # a sync call can't wait for a start that is still running; that thread closes the instance itself

    @override
    def expire_connections(self) -> None:
        if self._inner is not None:
            self._inner.expire_connections()

    @property
    @override
    def size(self) -> int:
        return 0 if self._inner is None else self._inner.size

    @property
    @override
    def idle(self) -> int:
        return 0 if self._inner is None else self._inner.idle

    @property
    @override
    def waiting(self) -> int:
        return 0 if self._inner is None else self._inner.waiting

    @property
    @override
    def min_size(self) -> int:
        return 0

    @property
    @override
    def max_size(self) -> int:
        return MAX_SIZE

    @property
    @override
    def closed(self) -> bool:
        return self._terminated if self._inner is None else self._inner.closed

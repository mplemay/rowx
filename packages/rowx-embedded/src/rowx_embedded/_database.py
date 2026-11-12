import asyncio
import os
from concurrent.futures import Future
from contextlib import suppress
from threading import Lock, Thread
from typing import TYPE_CHECKING, Final, override

from rowx_core.errors import InterfaceError, PoolTimeoutError
from rowx_postgres import ConnectOptions, Pool as BasePool

# `Database` is a `Pool` that builds its native pool late, through the protected hooks of `Pool`, so it needs the
# internals that it shares a version with
from rowx_postgres._pool import DEFAULT_ACQUIRE_TIMEOUT, Hook  # ruff: ignore[import-private-name]

from rowx_embedded._instance import (
    RESERVED_PARAMETERS,
    EmbeddedDatabase,
    abandon_if_cancelled,
    check_directory,
    start_blocking,
    with_socket,
)

if TYPE_CHECKING:
    from os import PathLike

    from rowx_postgres._core import Pool as PoolImpl

# an embedded instance serves one session at a time
MAX_SIZE: Final[int] = 1
CLOSED: Final[str] = "the pool is closed"


def check_directory_argument(directory: str | PathLike[str] | None, /) -> str | None:
    return None if directory is None else check_directory(os.fspath(directory))


def check_options(options: ConnectOptions | None, /) -> ConnectOptions:
    resolved = ConnectOptions() if options is None else options
    # `hostaddr` is not an option field, so only the names that `ConnectOptions` has are looked up
    for name in sorted(RESERVED_PARAMETERS):
        if getattr(resolved, name, None) is not None:
            msg = f"{name} cannot be set for an embedded database"
            raise ValueError(msg)
    return resolved


class Database(BasePool):
    def __init__(  # ruff: ignore[too-many-arguments]
        self,
        directory: str | PathLike[str] | None = None,
        /,
        *,
        options: ConnectOptions | None = None,
        acquire_timeout: float = DEFAULT_ACQUIRE_TIMEOUT,
        init: Hook | None = None,
        setup: Hook | None = None,
        reset: Hook | None = None,
    ) -> None:
        self._target = check_directory_argument(directory)
        resolved = check_options(options)
        # `min_size` is 0 because a native `min_size` of 1 would hold the only session as soon as the pool opens
        super().__init__(
            resolved,
            min_size=0,
            max_size=MAX_SIZE,
            acquire_timeout=acquire_timeout,
            init=init,
            setup=setup,
            reset=reset,
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
            msg = CLOSED
            raise InterfaceError(msg)
        if database is None:
            msg = "the embedded database has not started; call open() first"
            raise InterfaceError(msg)
        return database.dsn

    def _check_process(self) -> None:
        if os.getpid() == self._pid:
            return
        # a database that never started owns nothing of the parent's, so a forked child may start it for itself
        if self._start is not None or self._database is not None:
            msg = "the database was started in another process; create the database after the fork"
            raise InterfaceError(msg)
        # the parent's lock may have been held by one of its other threads at the fork
        self._state_lock = Lock()
        self._pid = os.getpid()

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
                msg = CLOSED
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
            msg = CLOSED
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
        msg = CLOSED
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
        if database is None:
            pass
        elif database.directory is None:
            # a memory database holds nothing worth waiting for, and a close would block the caller (often the event
            # loop) for as long as a running statement does
            database.abandon()
        else:
            # a directory is always closed properly, which does wait for a running statement
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

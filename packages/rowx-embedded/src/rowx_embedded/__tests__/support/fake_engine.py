import asyncio
import os
import threading
import warnings
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Callable

SOCKET_DSN: Final[str] = "postgresql://postgres@/postgres?host=/nonexistent/rowx-socket&sslmode=disable"
WAIT_SECONDS: Final[float] = 10.0


class FakeEngineError(Exception):
    pass


class FakeDirectoryInUseError(FakeEngineError):
    pass


class FakeSnapshotError(FakeEngineError):
    pass


class FakeInstance:
    def __init__(self, directory: str | None, snapshot: bytes | None) -> None:
        self.directory = directory
        self.booted_from = snapshot
        self.dsn = SOCKET_DSN
        self.closes = 0
        self.abandoned = False
        self.closed_event = threading.Event()
        self.entered_close = threading.Event()
        self.close_gate: threading.Event | None = None
        self.snapshot_bytes = b"snapshot"
        self.abandon_forced = False

    def snapshot(self) -> bytes:
        return self.snapshot_bytes

    def abandon(self, *, force: bool = False) -> None:
        self.abandon_forced = force
        self.abandoned = True
        self.closed_event.set()

    def close(self) -> None:
        self.closes += 1
        if self.close_gate is not None:
            assert self.close_gate.wait(WAIT_SECONDS)
        self.closed_event.set()


# stands in for the engine's `Instance` class (`start`) and its error types
class FakeEngine:
    def __init__(self) -> None:
        self.EngineError = FakeEngineError
        self.DirectoryInUseError = FakeDirectoryInUseError
        self.SnapshotError = FakeSnapshotError
        self.started: list[FakeInstance] = []
        self.failure: Exception | None = None
        self.gate: threading.Event | None = None
        self.entered = threading.Event()

    def start(self, directory: str | None = None, snapshot: bytes | None = None) -> FakeInstance:
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(WAIT_SECONDS)
        if self.failure is not None:
            raise self.failure
        instance = FakeInstance(directory, snapshot)
        self.started.append(instance)
        return instance


class FakeConnection:
    def __init__(self) -> None:
        self.closes = 0

    async def close(self) -> None:
        self.closes += 1

    @property
    def closed(self) -> bool:
        return self.closes > 0


def run[T](factory: Callable[[], Any]) -> T:
    return asyncio.run(factory())


def fork_and_run(child: Callable[[], int]) -> int:
    with warnings.catch_warnings():
        # forking a process that may have other threads is what these tests are about
        warnings.simplefilter("ignore", DeprecationWarning)
        pid = os.fork()
    if pid == 0:
        code = 3
        try:
            code = child()
        finally:
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)

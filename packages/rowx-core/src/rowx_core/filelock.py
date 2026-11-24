import asyncio
import errno
import os
import sys
from contextlib import suppress
from typing import TYPE_CHECKING, Final

if sys.platform == "win32":
    import msvcrt
else:
    from fcntl import LOCK_EX, LOCK_NB, flock

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from pathlib import Path

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

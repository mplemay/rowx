import asyncio
import threading
from pathlib import Path

import pytest
from rowx_core.filelock import finish, lock, try_lock, unlock


def open_descriptors() -> int:
    return sum(1 for _ in Path("/dev/fd").iterdir())


def test_the_lock_is_exclusive_until_released(tmp_path: Path) -> None:
    path = tmp_path / "p.lock"
    first = try_lock(path)
    assert first is not None
    assert try_lock(path) is None
    unlock(first)
    second = try_lock(path)
    assert second is not None
    unlock(second)


def test_unlocking_twice_is_harmless(tmp_path: Path) -> None:
    descriptor = try_lock(tmp_path / "p.lock")
    assert descriptor is not None
    unlock(descriptor)
    unlock(descriptor)


def test_a_failure_to_open_the_lock_file_is_raised(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        try_lock(tmp_path / "missing" / "p.lock")


@pytest.mark.skipif(not Path("/dev/fd").is_dir(), reason="counts descriptors through /dev/fd")
def test_a_cancelled_wait_for_the_lock_leaks_no_descriptor(tmp_path: Path) -> None:
    path = tmp_path / "p.lock"
    held = try_lock(path)
    assert held is not None

    async def scenario() -> None:
        before = open_descriptors()
        waiting = asyncio.create_task(lock(path))
        await asyncio.sleep(0.2)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert open_descriptors() == before

    asyncio.run(scenario())
    unlock(held)
    # the lock was never taken by the cancelled waiter
    again = try_lock(path)
    assert again is not None
    unlock(again)


def test_a_waiting_lock_is_taken_once_released(tmp_path: Path) -> None:
    path = tmp_path / "p.lock"
    held = try_lock(path)
    assert held is not None

    async def scenario() -> int:
        waiting = asyncio.create_task(lock(path))
        await asyncio.sleep(0.1)
        assert not waiting.done()
        unlock(held)
        return await waiting

    unlock(asyncio.run(scenario()))


def test_finish_returns_the_result_of_the_work() -> None:
    async def scenario() -> int:
        return await finish(asyncio.to_thread(lambda: 7))

    assert asyncio.run(scenario()) == 7


def test_a_cancel_waits_for_the_thread_to_finish() -> None:
    started = threading.Event()
    release = threading.Event()
    done = threading.Event()

    def work() -> None:
        started.set()
        release.wait()
        done.set()

    async def scenario() -> None:
        running = asyncio.create_task(finish(asyncio.to_thread(work)))
        await asyncio.to_thread(started.wait)
        running.cancel()
        try:
            # the cancel is held until the thread is let go
            await asyncio.sleep(0.1)
            assert not running.done()
        finally:
            # a failed assertion must not leave the thread blocked, or the run would hang at shutdown
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert done.is_set()

    asyncio.run(scenario())

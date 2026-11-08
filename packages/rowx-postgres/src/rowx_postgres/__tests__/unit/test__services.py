import asyncio
from itertools import pairwise
from types import SimpleNamespace

import pytest
from rowx_core.errors import DatabaseError, InterfaceError
from rowx_postgres import ConnectOptions
from rowx_postgres._services import wait_for_server


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        await self.spend(seconds)

    async def spend(self, seconds: float) -> None:
        self.now += seconds


# raises `errors` in turn (`None` connects, then every attempt is refused), each attempt taking `cost` seconds, and
# keeps the timeout each attempt was given
def run_attempts(
    monkeypatch: pytest.MonkeyPatch,
    errors: list[Exception | None],
    seconds: float,
    *,
    cost: float = 0.0,
    options: ConnectOptions | None = None,
) -> tuple[list[float | None], Exception | None, Clock]:
    clock = Clock()
    monkeypatch.setattr("rowx_postgres._services.monotonic", clock.monotonic)
    # replace the module binding so the patch does not change the shared asyncio.sleep
    monkeypatch.setattr("rowx_postgres._services.asyncio", SimpleNamespace(sleep=clock.sleep))
    timeouts: list[float | None] = []

    async def attempt(given: ConnectOptions) -> str:
        timeouts.append(given.connect_timeout)
        await clock.spend(cost)
        if (error := errors.pop(0) if errors else InterfaceError("refused")) is not None:
            raise error
        return "connected"

    try:
        assert asyncio.run(wait_for_server(attempt, options or ConnectOptions(), seconds)) == "connected"
    except (InterfaceError, DatabaseError) as error:
        return timeouts, error, clock
    return timeouts, None, clock


def test_wait_for_server_retries_refusals_and_starting_up_then_returns_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    errors: list[Exception | None] = [InterfaceError("refused"), DatabaseError("57P03", "starting up"), None]
    timeouts, error, clock = run_attempts(monkeypatch, errors, 10)
    assert error is None
    assert len(timeouts) == 3
    assert clock.slept == [0.05, 0.1]


def test_wait_for_server_does_not_retry_other_server_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    timeouts, error, _ = run_attempts(monkeypatch, [DatabaseError("28P01", "password authentication failed")], 10)
    assert isinstance(error, DatabaseError)
    assert error.sqlstate == "28P01"
    assert len(timeouts) == 1


def test_a_zero_timeout_makes_one_attempt_with_the_urls_own_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    timeouts, error, clock = run_attempts(monkeypatch, [], 0, options=ConnectOptions(connect_timeout=3))
    assert isinstance(error, InterfaceError)
    assert timeouts == [3]
    assert clock.slept == []


def test_each_attempt_is_bounded_by_the_time_left_and_none_starts_after_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # every attempt hangs until its own timeout
    timeouts, error, clock = run_attempts(monkeypatch, [], 2, cost=0.4)
    assert isinstance(error, InterfaceError)
    limits = [timeout for timeout in timeouts if timeout is not None and timeout > 0]
    assert len(limits) == len(timeouts) > 1
    assert limits[0] == pytest.approx(2)
    assert all(later < earlier for earlier, later in pairwise(limits))
    # an attempt's timeout is the time left when it started, so none started at or after the deadline
    assert clock.now <= 2 + 0.4


def test_bounded_keeps_a_shorter_timeout_from_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    for configured, expected in ((None, 5), (2, 2), (8, 5)):
        timeouts, error, _ = run_attempts(monkeypatch, [None], 5, options=ConnectOptions(connect_timeout=configured))
        assert error is None
        assert timeouts == [pytest.approx(expected)]
    # Even a nearly expired attempt has a positive driver timeout.
    timeouts, error, _ = run_attempts(monkeypatch, [None], 0.0001)
    assert error is None
    assert timeouts[0] is not None
    assert timeouts[0] > 0

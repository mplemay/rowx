import asyncio
from argparse import ArgumentTypeError, Namespace
from itertools import pairwise
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from rowx.check.cli import _parser
from rowx.check.config import MigrateConfig
from rowx.check.migrate import (
    DEFAULT_CONNECT_TIMEOUT,
    Target,
    _load,
    bounded,
    connect_timeout_type,
    load_migrator,
    wait_for_server,
)
from rowx.errors import DatabaseError, InterfaceError
from rowx.postgres import ConnectOptions

if TYPE_CHECKING:
    from pathlib import Path


def test_connect_timeout_defaults_to_ten_seconds_on_migrate_and_database() -> None:
    assert pytest.approx(10.0) == DEFAULT_CONNECT_TIMEOUT
    parser = _parser()
    for argv in (
        ["migrate", "run"],
        ["migrate", "revert"],
        ["migrate", "skip"],
        ["migrate", "info"],
        ["database", "create"],
        ["database", "drop"],
        ["database", "reset"],
        ["database", "setup"],
    ):
        assert parser.parse_args(argv).connect_timeout == pytest.approx(DEFAULT_CONNECT_TIMEOUT)
    assert parser.parse_args(["migrate", "info", "--connect-timeout", "2.5"]).connect_timeout == pytest.approx(2.5)


@pytest.mark.parametrize("value", ["-1", "abc", "nan", "inf"])
def test_connect_timeout_rejects_bad_values(value: str) -> None:
    with pytest.raises(ArgumentTypeError):
        connect_timeout_type(value)


def test_revert_all_excludes_a_target() -> None:
    parser = _parser()
    assert parser.parse_args(["migrate", "revert", "--all"]).all is True
    with pytest.raises(SystemExit) as exit_info:
        parser.parse_args(["migrate", "revert", "--all", "--target-version", "1"])
    assert exit_info.value.code == 2


def test_skip_takes_the_run_flags() -> None:
    args = _parser().parse_args(
        ["migrate", "skip", "--target-version", "3", "--dry-run", "--allow-out-of-order", "--ignore-missing"],
    )
    assert (args.target_version, args.dry_run, args.allow_out_of_order, args.ignore_missing) == (3, True, True, True)


def test_info_output_format() -> None:
    parser = _parser()
    assert parser.parse_args(["migrate", "info"]).output_format == "text"
    assert parser.parse_args(["migrate", "info", "--output-format", "json"]).output_format == "json"


@pytest.mark.parametrize(
    ("config", "flag", "expected"),
    [(False, None, False), (True, None, True), (False, True, True), (True, False, False)],
)
def test_a_flag_beats_the_config_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    config: bool,
    flag: bool | None,
    expected: bool,
) -> None:
    seen: list[dict[str, object]] = []

    def fake(_directory: Path, **kwargs: object) -> None:
        seen.append(kwargs)

    monkeypatch.setattr("rowx.postgres._backend.Migrator", fake)
    target = Target(directory=tmp_path, migrate=MigrateConfig(ignore_missing=config, allow_out_of_order=config))
    _load(target, Namespace(ignore_missing=flag, allow_out_of_order=flag))
    assert [(call["ignore_missing"], call["allow_out_of_order"]) for call in seen] == [(expected, expected)]
    # `--scratch`, the plugin and `database setup` load the migrator with the config's policy
    seen.clear()
    load_migrator(target)
    assert [(call["ignore_missing"], call["allow_out_of_order"]) for call in seen] == [(config, config)]


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
    monkeypatch.setattr("rowx.check.migrate.monotonic", clock.monotonic)
    # replace the module binding so the patch does not change the shared asyncio.sleep
    monkeypatch.setattr("rowx.check.migrate.asyncio", SimpleNamespace(sleep=clock.sleep))
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


def test_bounded_keeps_a_shorter_timeout_from_the_url() -> None:
    assert bounded(ConnectOptions(), 5).connect_timeout == pytest.approx(5)
    assert bounded(ConnectOptions(connect_timeout=2), 5).connect_timeout == pytest.approx(2)
    assert bounded(ConnectOptions(connect_timeout=8), 5).connect_timeout == pytest.approx(5)
    # the driver reads zero as no timeout, so the time left never rounds down to it
    timeout = bounded(ConnectOptions(), 0).connect_timeout
    assert timeout is not None
    assert timeout > 0

from argparse import ArgumentTypeError, Namespace
from typing import TYPE_CHECKING

import pytest

from rowx.check.cli import _parser
from rowx.check.config import MigrateConfig
from rowx.check.migrate import DEFAULT_CONNECT_TIMEOUT, Target, _load, connect_timeout_type

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

    monkeypatch.setattr("rowx.check.migrate.Migrator", fake)
    target = Target(directory=tmp_path, migrate=MigrateConfig(ignore_missing=config, allow_out_of_order=config))
    _load(target, Namespace(ignore_missing=flag, allow_out_of_order=flag))
    assert [(call["ignore_missing"], call["allow_out_of_order"]) for call in seen] == [(expected, expected)]

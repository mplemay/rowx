import io
from pathlib import Path
from typing import TYPE_CHECKING, override

import pytest

from rowx.check.cli import _parser, main
from rowx.check.database import _confirmed
from rowx.check.runner import CheckError

if TYPE_CHECKING:
    from argparse import Namespace


class FakeStdin(io.StringIO):
    def __init__(self, text: str = "", *, tty: bool) -> None:
        super().__init__(text)
        self.tty = tty

    @override
    def isatty(self) -> bool:
        return self.tty


def parse(*argv: str) -> Namespace:
    return _parser().parse_args(["database", *argv])


def test_parses_flags() -> None:
    args = parse("drop", "--force", "-y", "--database-url", "u")
    assert (args.database_command, args.force, args.yes, args.database_url) == ("drop", True, True, "u")
    assert parse("reset").force is False
    assert parse("setup", "--source", "d").source == Path("d")


@pytest.mark.parametrize(
    "argv", [["create", "-y"], ["create", "--force"], ["setup", "--force"], ["create", "--source", "d"]]
)
def test_rejects_flags_that_do_not_apply(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        parse(*argv)
    assert raised.value.code == 2


def test_requires_a_subcommand() -> None:
    with pytest.raises(SystemExit):
        parse()


def test_yes_skips_the_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin(tty=False))
    assert _confirmed("drop", "app", yes=True)


def test_no_tty_without_yes_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin("y\n", tty=False))
    with pytest.raises(CheckError, match="pass -y"):
        _confirmed("drop", "app", yes=False)


@pytest.mark.parametrize(("answer", "expected"), [("y\n", True), ("YES\n", True), ("n\n", False), ("\n", False)])
def test_tty_prompt(
    answer: str, *, expected: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin(answer, tty=True))
    assert _confirmed("drop", "app", yes=False) is expected
    assert "Drop database 'app'? [y/N]" in capsys.readouterr().out


def test_no_database_url_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main(["database", "create"]) == 2
    assert "no database URL" in capsys.readouterr().err


def test_unreachable_database_exits_two_without_the_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    url = "postgresql://role:hunter2pw@127.0.0.1:1/app"
    assert main(["database", "create", "--database-url", url]) == 2
    captured = capsys.readouterr()
    assert "hunter2pw" not in captured.out + captured.err
    assert url not in captured.out + captured.err


def test_url_without_a_database_name_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["database", "drop", "-y", "--database-url", "postgresql://role:pw@127.0.0.1:1/"]) == 2
    assert "names no database" in capsys.readouterr().err

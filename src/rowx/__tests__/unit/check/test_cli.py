from argparse import Namespace
from asyncio import sleep
from typing import Final

import pytest

from rowx.check import cli
from rowx.check.migrate import run_guarded
from rowx.check.runner import CheckError
from rowx.errors import InterfaceError

HOSTILE: Final = f"bad \x1b[2J{chr(0x202E)} text"


def test_cli_errors_are_stripped_of_control_and_bidi_characters(capsys: pytest.CaptureFixture[str]) -> None:
    def handler(_: Namespace) -> None:
        raise CheckError(HOSTILE)

    assert cli._subcommand(handler, Namespace()) == cli.EXIT_FAILURE
    assert capsys.readouterr().err == "error: bad [2J text\n"


def test_database_errors_are_stripped_after_redaction() -> None:
    async def work() -> None:
        await sleep(0)
        raise InterfaceError(HOSTILE)

    with pytest.raises(CheckError) as caught:
        run_guarded("host=h", work())
    assert str(caught.value) == "could not reach the database: bad [2J text"

from importlib.metadata import version

import pytest

from rowx.check.cli import main


def test_check_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["check", "--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == version("rowx")

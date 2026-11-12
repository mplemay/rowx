import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
from typing import Final

import rowx_embedded

from rowx import embedded

BLOCKED: Final[str] = "import sys\nsys.modules['rowx_embedded'] = None\nimport rowx.embedded\n"


def test_the_facade_without_the_companion_says_how_to_install_it() -> None:
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [sys.executable, "-I", "-c", BLOCKED],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert 'Install "rowx[embedded]" to use rowx.embedded.' in result.stderr


def test_the_facade_exposes_the_database_of_the_companion() -> None:
    assert embedded.Database is rowx_embedded.Database
    assert embedded.__all__ == ["Database"]

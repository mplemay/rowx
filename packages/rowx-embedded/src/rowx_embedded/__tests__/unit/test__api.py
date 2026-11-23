import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
from typing import Final

# a `None` entry in `sys.modules` makes the import of the engine raise `ImportError`
BROKEN_ENGINE: Final[str] = """
import sys

sys.modules["rowx_embedded._engine"] = None
try:
    import rowx_embedded._api
except ImportError as error:
    print(error)
    print(isinstance(error.__cause__, ImportError))
    raise SystemExit(0)
raise SystemExit(1)
"""


# `_api` registers exit handlers and replaces `sys.excepthook`, so the import runs in a child process
def test_an_engine_that_will_not_load_is_a_chained_import_error() -> None:
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [sys.executable, "-c", BROKEN_ENGINE],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    message, cause = result.stdout.splitlines()
    assert message.startswith("the embedded engine could not be loaded: ")
    assert cause == "True"

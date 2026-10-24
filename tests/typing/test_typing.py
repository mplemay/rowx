import json
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

TYPING_DIR: Final[Path] = Path(__file__).resolve().parent
ROOT_DIR: Final[Path] = TYPING_DIR.parent.parent
CASES: Final[Path] = TYPING_DIR / "cases.py"

REVEAL_COMMENT: Final[re.Pattern[str]] = re.compile(r"# reveal: (?P<type>.+)$")
ERROR_COMMENT: Final[re.Pattern[str]] = re.compile(r"# error(?:\s|$)")
PYRIGHT_REVEAL: Final[re.Pattern[str]] = re.compile(r'^Type of ".*" is "(?P<type>.+)"$', re.DOTALL)
MYPY_LINE: Final[re.Pattern[str]] = re.compile(
    r"^.+?:(?P<line>\d+):\d+: (?P<severity>note|error): (?P<message>.*)$",
)
MYPY_REVEAL: Final[re.Pattern[str]] = re.compile(r'^Revealed type is "(?P<type>.+)"$')
# mypy qualifies classes with their module (`decimal.Decimal`, `rowx._query.query[int]`) and pyright doesn't
MODULE_PREFIX: Final[re.Pattern[str]] = re.compile(r"\b(?:\w+\.)+(?=\w)")


def _normalize(type_: str) -> str:
    return MODULE_PREFIX.sub("", type_)


def _expectations() -> tuple[dict[int, str], set[int]]:
    reveals: dict[int, str] = {}
    errors: set[int] = set()
    for number, line in enumerate(CASES.read_text().splitlines(), start=1):
        if match := REVEAL_COMMENT.search(line):
            reveals[number] = match["type"]
        elif ERROR_COMMENT.search(line):
            errors.add(number)
    return reveals, errors


def _run_pyright() -> tuple[dict[int, str], set[int]]:
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pyright", "--outputjson", "--pythonversion", "3.14", str(CASES)],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT_DIR,
    )
    reveals: dict[int, str] = {}
    errors: set[int] = set()
    for diagnostic in json.loads(result.stdout)["generalDiagnostics"]:
        line = diagnostic["range"]["start"]["line"] + 1
        if diagnostic["severity"] == "error":
            errors.add(line)
        elif match := PYRIGHT_REVEAL.match(diagnostic["message"]):
            reveals[line] = _normalize(match["type"])
    return reveals, errors


def _run_mypy() -> tuple[dict[int, str], set[int]]:
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "mypy",
            "--python-version",
            "3.14",
            "--no-error-summary",
            "--show-column-numbers",
            str(CASES),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT_DIR,
    )
    reveals: dict[int, str] = {}
    errors: set[int] = set()
    for raw in result.stdout.splitlines():
        if not (match := MYPY_LINE.match(raw)):
            continue
        line = int(match["line"])
        if match["severity"] == "error":
            errors.add(line)
        elif reveal := MYPY_REVEAL.match(match["message"]):
            reveals[line] = _normalize(reveal["type"])
    return reveals, errors


type Diagnostics = tuple[dict[int, str], set[int]]


@pytest.mark.parametrize("runner", [_run_pyright, _run_mypy], ids=["pyright", "mypy"])
def test_cases(runner: Callable[[], Diagnostics]) -> None:
    expected_reveals, expected_errors = _expectations()
    if not expected_reveals or not expected_errors:
        pytest.fail("cases.py has no reveal or error expectations")
    reveals, errors = runner()
    if reveals != expected_reveals:
        pytest.fail(f"reveal mismatch\nexpected: {expected_reveals}\nactual:   {reveals}")
    if errors != expected_errors:
        pytest.fail(f"error mismatch\nexpected: {sorted(expected_errors)}\nactual:   {sorted(errors)}")

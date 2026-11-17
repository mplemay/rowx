from typing import TYPE_CHECKING, Final, get_args

import pytest
from rowx_postgres.__tests__.integration.check.harness import CASES_DIR, check_case, parse_markers

from rowx.check.cli import main
from rowx.check.model import Rule

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = [pytest.mark.db]

CASE_FILES: Final[list[Path]] = sorted(path for path in CASES_DIR.rglob("*.py") if path.name != "__init__.py")
# a file that does not parse cannot live in the tree, because the linters would reject it
SYNTAX_ERROR: Final[str] = "from rowx import query\n\nquery[int](t'SELECT 1'  # error: [syntax-error]\n"


@pytest.mark.parametrize("path", CASE_FILES, ids=lambda path: str(path.relative_to(CASES_DIR)))
def test_case_matches_its_markers(path: Path, schema: str) -> None:
    assert check_case(path, schema) == []


def test_syntax_error_case(schema: str, tmp_path: Path) -> None:
    path = tmp_path / "broken.py"
    path.write_text(SYNTAX_ERROR, encoding="utf-8")
    assert check_case(path, schema) == []


def test_every_rule_has_a_marker() -> None:
    rules = set(get_args(Rule.__value__))
    marked = {expectation.rule for path in CASE_FILES for expectation in parse_markers(path.read_text("utf-8"))}
    marked.update(expectation.rule for expectation in parse_markers(SYNTAX_ERROR))
    # Policy and suppression diagnostics are exercised by checker CLI and suppression tests.
    assert rules - marked == {
        "coverage-require-checked",
        "coverage-unchecked",
        "invalid-ignore-comment",
        "unused-ignore-comment",
    }
    assert marked - rules == set()


def test_query_unchecked_is_skipped_and_counted(schema: str, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["check", str(CASES_DIR / "query_unchecked.py"), "--database-url", schema, "--statistics"])
    out = capsys.readouterr().out
    assert code == 0
    assert "query_unchecked calls skipped: 2\n" in out
    assert "call sites checked: 1\n" in out

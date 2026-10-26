import ast
import asyncio
import warnings
from pathlib import Path
from typing import Final

import pytest

from rowx.__tests__.sourcegen import Case, build_case, error_atoms, line_col, seeded
from rowx.check.discover import discover_source
from rowx.check.runner import describe_all
from rowx.check.sourcemap import SourceMap, SourceMapper
from rowx.errors import DatabaseError

pytestmark = [pytest.mark.integration, pytest.mark.db]

SEEDS: Final = range(120)
TIMEOUT_MS: Final[int] = 10_000
UNDEFINED_COLUMN: Final[str] = "42703"
# the error sits in the second static segment, after the first interpolation
ERROR_SEGMENT: Final[int] = 1


def template_of(case: Case) -> ast.TemplateStr:
    with warnings.catch_warnings():
        # a generated unknown escape is a SyntaxWarning, which pytest turns into an error
        warnings.simplefilter("ignore", SyntaxWarning)
        result = discover_source(Path("m.py"), case.source.encode())
    assert not result.diagnostics
    template = result.calls[0].template
    assert template is not None
    return template


# swaps the first character inside the leading `/* ` of the error segment, so the decoded text no longer matches the
# source and the segment becomes inexact without changing where the error is
def corrupt(template: ast.TemplateStr) -> None:
    seen = 0
    for index, value in enumerate(template.values):
        if isinstance(value, ast.Interpolation):
            seen += 1
        elif seen == ERROR_SEGMENT and isinstance(value, ast.Constant) and isinstance(value.value, str):
            at = value.value.index("/* ") + len("/* ")
            template.values[index] = ast.Constant(value.value[:at] + "~" + value.value[at + 1 :])
            return
    pytest.fail("the error segment has no constant")


def expected_error_span(
    case: Case, sourcemap: SourceMap, *, corrupted: bool
) -> tuple[tuple[int, int], tuple[int, int]]:
    placed, segment = case.marks["ZZBAD"]
    assert segment == ERROR_SEGMENT
    if corrupted:
        # an inexact segment maps every caret to the segment start
        assert sourcemap.exact == (True, False, True)
        start = line_col(case.source, case.starts[segment])
        return start, start
    assert all(sourcemap.exact)
    return line_col(case.source, placed.offset), line_col(case.source, placed.offset + placed.length)


@pytest.mark.parametrize("corrupted", [False, True], ids=["exact", "inexact"])
def test_server_error_positions_map_to_the_source(schema: str, corrupted: bool) -> None:  # ruff: ignore[boolean-type-hint-positional-argument]
    cases: list[tuple[Case, SourceMap]] = []
    for seed in SEEDS:
        rng = seeded(seed)
        case = build_case(rng, error_atoms(rng))
        template = template_of(case)
        if corrupted:
            corrupt(template)
        cases.append((case, SourceMapper(case.source).build(template)))
    statements = sorted({sourcemap.sql for _, sourcemap in cases})
    outcomes = asyncio.run(describe_all(schema, statements, 1, TIMEOUT_MS))
    for case, sourcemap in cases:
        error = outcomes[sourcemap.sql]
        assert isinstance(error, DatabaseError), sourcemap.sql
        assert error.sqlstate == UNDEFINED_COLUMN, (error.message, sourcemap.sql)
        assert error.position is not None
        # the server counts characters, and the error is at the start of the marked token
        assert sourcemap.sql[error.position - 1 :].startswith("ZZBAD")
        span = sourcemap.locate(error.position)
        start, end = expected_error_span(case, sourcemap, corrupted=corrupted)
        assert ((span.line, span.col), (span.end_line, span.end_col)) == (start, end), case.source

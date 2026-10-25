import ast
import asyncio
from typing import TYPE_CHECKING, Final

import psycopg
import pytest

from rowx.__tests__.integration.check.harness import CASES_DIR, parse_markers
from rowx._core.postgres import Describe
from rowx.check.discover import discover_source
from rowx.check.runner import describe_all
from rowx.check.sourcemap import SourceMapper

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = [pytest.mark.integration, pytest.mark.db]

CASE_FILES: Final[list[Path]] = sorted(path for path in CASES_DIR.rglob("*.py") if path.name != "__init__.py")
TIMEOUT_MS: Final[int] = 10_000


# a query is an OK case when its call line carries no marker; one with parameters has no values to run with
def _ok_statements(path: Path) -> list[str]:
    data = path.read_bytes()
    marked = {expectation.line for expectation in parse_markers(data.decode("utf-8"))}
    result = discover_source(path, data)
    if result.source is None:
        return []
    mapper = SourceMapper(result.source)
    return [
        mapper.build(call.template).sql
        for call in result.calls
        if call.template is not None
        and call.span.line not in marked
        and not any(isinstance(part, ast.Interpolation) for part in call.template.values)
    ]


def test_not_null_columns_are_never_null(schema: str) -> None:
    statements = sorted({sql for path in CASE_FILES for sql in _ok_statements(path)})
    outcomes = asyncio.run(describe_all(schema, statements, 1, TIMEOUT_MS))
    guarded = 0
    with psycopg.connect(schema, autocommit=False) as conn:
        for sql in statements:
            described = outcomes[sql]
            assert isinstance(described, Describe), f"{sql}: {described}"
            try:
                cursor = conn.execute(sql)  # ty: ignore[no-matching-overload]
                rows = cursor.fetchall() if cursor.description is not None else []
            finally:
                conn.rollback()
            for index, column in enumerate(described.columns):
                if column.nullable:
                    continue
                guarded += 1
                assert all(row[index] is not None for row in rows), f"{sql}: column {column.name!r} is NULL"
    assert guarded > 0

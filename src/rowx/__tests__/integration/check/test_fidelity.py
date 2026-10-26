import asyncio
from dataclasses import dataclass
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


# seed-drawn literals for each parameter type: values that match rows in `tests/seed_data.sql` first, then ones
# that match nothing. NULL is always tried as well
SEED_LITERALS: Final[dict[str, tuple[str, ...]]] = {
    "int2": ("1", "0"),
    "int4": ("1", "0", "2"),
    "int8": ("1", "0"),
    "oid": ("1", "0"),
    "float4": ("10.5", "0"),
    "float8": ("10.5", "0"),
    "numeric": ("10.50", "0", "42"),
    "bool": ("true", "false"),
    "text": ("'alice'", "'alice@example.com'", "'orphan note'", "'nobody'"),
    "varchar": ("'alice'", "'nobody'"),
    "bpchar": ("'alice'", "'nobody'"),
    "name": ("'alice'", "'nobody'"),
    "citext": ("'ALICE'", "'nobody'"),
    "uuid": ("'00000000-0000-0000-0000-000000000001'", "'00000000-0000-0000-0000-000000000002'"),
    "date": ("'2026-10-23'", "'1999-01-01'"),
    "timestamp": ("'2026-10-23 12:34:56'", "'1999-01-01'"),
    "timestamptz": ("'2026-10-23 12:34:56+00'", "'1999-01-01'"),
    "json": ("'{\"a\": 1}'",),
    "jsonb": ("'{\"a\": 1}'", "'{}'"),
    "_int4": ("'{1,2}'", "'{}'", "'{NULL}'"),
    "_text": ("'{alice,bob}'", "'{}'", "'{NULL}'"),
}


@dataclass(slots=True, frozen=True)
class Statement:
    sql: str
    path: str


# a query is an OK case when its call line carries no marker
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
        if call.template is not None and call.span.line not in marked
    ]


# every parameter at its first seed value, then each one varied over the rest of its candidates and NULL, and
# finally all NULL
def _bindings(candidates: list[tuple[str, ...]]) -> list[list[str]]:
    base = [options[0] if options else "NULL" for options in candidates]
    found = [base, ["NULL"] * len(candidates)]
    for index, options in enumerate(candidates):
        found.extend([*base[:index], value, *base[index + 1 :]] for value in (*options[1:], "NULL"))
    unique: list[list[str]] = []
    for binding in found:
        if binding not in unique:
            unique.append(binding)
    return unique


def _statements() -> list[str]:
    return sorted({sql for path in CASE_FILES for sql in _ok_statements(path)})


def _describe(dsn: str, statements: list[str]) -> dict[str, Describe]:
    outcomes = asyncio.run(describe_all(dsn, statements, 1, TIMEOUT_MS))
    described: dict[str, Describe] = {}
    for sql in statements:
        outcome = outcomes[sql]
        assert isinstance(outcome, Describe), f"{sql}: {outcome}"
        described[sql] = outcome
    return described


# a `!` override is the user's assertion, not describe's verdict, so a NULL parameter may legitimately break it
def _assert_not_null_holds(cursor: psycopg.Cursor, sql: str, described: Describe, how: str, *, skip_bang: bool) -> int:
    rows = cursor.fetchall() if cursor.description is not None else []
    guarded = 0
    for index, column in enumerate(described.columns):
        if column.nullable or (skip_bang and column.reason == "OverrideBang"):
            continue
        guarded += 1
        assert all(row[index] is not None for row in rows), f"{sql} [{how}]: column {column.name!r} is NULL"
    return guarded


def test_not_null_columns_are_never_null(schema: str) -> None:
    statements = _statements()
    described = _describe(schema, statements)
    guarded = 0
    with psycopg.connect(schema, autocommit=False) as conn:
        for sql in statements:
            if described[sql].params:
                continue
            try:
                cursor = conn.execute(sql)  # ty: ignore[no-matching-overload]
                guarded += _assert_not_null_holds(cursor, sql, described[sql], "no parameters", skip_bang=False)
            finally:
                conn.rollback()
    assert guarded > 0


# describe plans generically; a custom plan can only remove NULLs relative to it, so what this finds is a column
# the catalog or an override wrongly calls NOT NULL, which some value (above all NULL) turns into NULL
@pytest.mark.parametrize("mode", ["force_custom_plan", "force_generic_plan"])
def test_not_null_columns_are_never_null_with_parameters(schema: str, mode: str) -> None:
    statements = _statements()
    described = _describe(schema, statements)
    executed = guarded = 0
    with psycopg.connect(schema, autocommit=False) as conn:
        for sql in statements:
            if not (params := described[sql].params):
                continue
            types = [
                conn.execute("SELECT format_type(%s, NULL)", (param.oid,)).fetchone()[0]  # ty: ignore[not-subscriptable]
                for param in params
            ]
            candidates = [SEED_LITERALS.get(param.type_name, ()) for param in params]
            for binding in _bindings(candidates):
                try:
                    conn.execute("SELECT set_config('plan_cache_mode', %s, true)", (mode,))
                    # a rollback does not undo PREPARE
                    conn.execute("DEALLOCATE ALL")
                    conn.execute(f"PREPARE rowx_fidelity ({', '.join(types)}) AS {sql}")  # ty: ignore[no-matching-overload]
                    cursor = conn.execute(f"EXECUTE rowx_fidelity ({', '.join(binding)})")  # ty: ignore[no-matching-overload]
                    executed += 1
                    guarded += _assert_not_null_holds(cursor, sql, described[sql], f"{mode} {binding}", skip_bang=True)
                finally:
                    conn.rollback()
    assert executed > 0
    assert guarded > 0

import asyncio
from dataclasses import dataclass, field
from string.templatelib import Template
from typing import Final

import pytest

import rowx
from rowx import query
from rowx.__tests__.conformance.row_types import PRELUDE, ROWS
from rowx._query import Connection
from rowx.errors import InterfaceError


@dataclass(slots=True)
class FakeImpl:
    fetches: list[tuple[list[bool], bool, int]] = field(default_factory=list)

    async def _fetch(
        self,
        _sql: str,
        _params: list[object],
        mask: list[bool],
        scalar: bool,  # noqa: FBT001
        _limit_one: bool,  # noqa: FBT001
        arity: int,
    ) -> list[object]:
        self.fetches.append((mask, scalar, arity))
        return []


def evaluate(source: str) -> object:
    ns: dict[str, object] = {}
    exec(PRELUDE + source, ns)  # noqa: S102
    return ns["T"]


def fetch_plan(t: object) -> tuple[str, int, tuple[bool, ...]]:
    impl = FakeImpl()
    conn = Connection(impl)  # ty: ignore[invalid-argument-type]
    asyncio.run(query[t](t"SELECT 1").fetch_all(conn))  # ty: ignore[invalid-type-form]
    ((mask, scalar, arity),) = impl.fetches
    return ("scalar" if scalar else "tuple", arity, tuple(mask))


@pytest.mark.parametrize(("source", "expected", "_verdict"), ROWS)
def test_conformance_row_at_first_fetch(
    source: str,
    expected: tuple[str, int, tuple[bool, ...]] | str,
    _verdict: str,
) -> None:
    t = evaluate(source)
    if isinstance(expected, str):
        with pytest.raises(InterfaceError):
            fetch_plan(t)
    elif expected[0] == "none":
        with pytest.raises(InterfaceError, match="without a row type"):
            fetch_plan(t)
    else:
        assert fetch_plan(t) == expected


DB_ROWS: Final[list[tuple[str, str]]] = [
    ("T = int", "SELECT 1"),
    ("T = tuple[int, str | None]", "SELECT 1, NULL::text"),
    ("T = list[int | None]", "SELECT ARRAY[1, NULL]::int[]"),
]
DB_EXPECTED: Final[list[object]] = [1, (1, None), [1, None]]


@pytest.mark.integration
@pytest.mark.db
@pytest.mark.parametrize(("row", "expected"), list(zip(DB_ROWS, DB_EXPECTED, strict=True)), ids=[r[0] for r in DB_ROWS])
def test_conformance_ok_row_against_db(schema: str, row: tuple[str, str], expected: object) -> None:
    source, sql = row
    t = evaluate(source)

    async def body() -> object:
        async with await rowx.connect(schema) as conn:
            return await query[t](Template(sql)).fetch_one(conn)  # ty: ignore[invalid-type-form]

    assert asyncio.run(body()) == expected

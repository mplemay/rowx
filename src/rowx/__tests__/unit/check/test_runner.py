import asyncio
from typing import Any

import pytest

from rowx.check import runner
from rowx.check.runner import CheckError, describe_all, quoted_aliases, redact
from rowx.errors import DatabaseError, InterfaceError


class FakeConnection:
    closed_count = 0

    async def close(self) -> None:
        type(self).closed_count += 1


@pytest.fixture(autouse=True)  # ruff: ignore[pytest-fixture-autouse]
def fake_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeConnection.closed_count = 0

    async def connect(_dsn: str, _timeout_ms: int) -> Any:  # ruff: ignore[any-type, unused-async]
        return FakeConnection()

    monkeypatch.setattr(runner, "_connect", connect)


def _describe_with(monkeypatch: pytest.MonkeyPatch, *outcomes: object) -> list[str]:
    seen: list[str] = []
    queue = list(outcomes)

    async def fake(_conn: object, sql: str) -> object:  # ruff: ignore[unused-async]
        seen.append(sql)
        outcome = queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(runner, "describe_impl", fake)
    return seen


def test_lost_connection_is_retried_once(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _describe_with(monkeypatch, DatabaseError("57P01", "terminating connection"), "described")
    results = asyncio.run(describe_all("postgresql://x", ["SELECT 1"], 1, 1000))
    assert results == {"SELECT 1": "described"}
    assert seen == ["SELECT 1", "SELECT 1"]
    assert FakeConnection.closed_count == 2


def test_connection_class_errors_are_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    _describe_with(monkeypatch, DatabaseError("08006", "connection failure"), InterfaceError("closed"), "described")
    with pytest.raises(CheckError, match="lost the database connection"):
        asyncio.run(describe_all("postgresql://x", ["SELECT 1"], 1, 1000))


def test_second_loss_fails_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    _describe_with(monkeypatch, InterfaceError("closed"), DatabaseError("57P01", "gone"))
    with pytest.raises(CheckError, match="lost the database connection"):
        asyncio.run(describe_all("postgresql://x", ["SELECT 1"], 1, 1000))


def test_timeout_fails_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    _describe_with(monkeypatch, DatabaseError("57014", "canceling statement due to statement timeout"))
    with pytest.raises(CheckError, match="timed out"):
        asyncio.run(describe_all("postgresql://x", ["SELECT 1"], 1, 1000))


def test_sql_errors_are_outcomes_not_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    error = DatabaseError("42P01", "relation does not exist")
    _describe_with(monkeypatch, error)
    assert asyncio.run(describe_all("postgresql://x", ["SELECT 1"], 1, 1000)) == {"SELECT 1": error}


def test_each_sql_is_described_once_across_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    sqls = [f"SELECT {n}" for n in range(9)]
    seen = _describe_with(monkeypatch, *[f"d{n}" for n in range(9)])
    results = asyncio.run(describe_all("postgresql://x", sqls, 4, 1000))
    assert sorted(seen) == sorted(sqls)
    assert set(results) == set(sqls)


@pytest.mark.parametrize(
    ("dsn", "secret"),
    [
        ("postgresql://user:p%40ss@host/db", "p%40ss"),
        ("postgresql://user:p%40ss@host/db", "p@ss"),
        ("host=h user=u password=p@ss dbname=d", "p@ss"),
        ("host=h user=u password='p ss' dbname=d", "p ss"),
        ("postgresql://u@h/db?password=pass&sslmode=require", "pass"),
    ],
)
def test_redact_removes_passwords(dsn: str, secret: str) -> None:
    redacted = redact(f"failed for {dsn} with {secret}", dsn)
    assert secret not in redacted
    assert dsn not in redacted


def test_aliases_come_only_from_as_clauses() -> None:
    sql = (
        'SELECT 1 AS "one!", 2 AS /* c */ "two?", "t"."col", \'AS "str!"\' AS x, $$ AS "dollar!" $$ AS y, '
        '3 -- AS "line!"\n AS "three!" /* AS "block!" /* nested */ AS "inner!" */, 4 AS "fo""ur!"'
    )
    assert [ident for _, ident in quoted_aliases(sql)] == ["one!", "two?", "three!", 'fo"ur!']
    assert [sql[offset] for offset, _ in quoted_aliases(sql)] == ['"'] * 4

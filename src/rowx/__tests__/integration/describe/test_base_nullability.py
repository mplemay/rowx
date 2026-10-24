import pytest

from rowx.__tests__.integration.describe.test_base import run_checker, run_describe
from rowx._core import _catalog_queries, describe

pytestmark = [pytest.mark.integration, pytest.mark.db]


def verdicts(dsn: str, sql: str) -> list[tuple[bool, str]]:
    return [(c.nullable, c.reason) for c in run_describe(dsn, sql).columns]


def test_table_columns(schema: str) -> None:
    assert verdicts(schema, "SELECT id, email FROM users") == [
        (False, "NotNullColumn"),
        (True, "NullableColumn"),
    ]


def test_computed_column(schema: str) -> None:
    assert verdicts(schema, "SELECT count(*) FROM users") == [(True, "Computed")]


def test_not_valid_constraint(schema: str) -> None:
    assert verdicts(schema, "SELECT x FROM not_valid") == [(True, "NotValidatedConstraint")]


def test_view_column(schema: str) -> None:
    assert verdicts(schema, "SELECT user_id, name FROM user_posts") == [
        (True, "ForeignOrView"),
        (True, "ForeignOrView"),
    ]


def test_duplicate_origin_columns(schema: str) -> None:
    assert verdicts(schema, "SELECT id, id AS again FROM users") == [(False, "NotNullColumn")] * 2


def test_one_catalog_round_trip(schema: str) -> None:
    sql = (
        "SELECT u.id, u.name, u.email, p.id, p.user_id, p.title, o.id, o.amount, o.note, 1 AS one "
        "FROM users u, posts p, orders o"
    )

    async def body(conn) -> int:
        before = _catalog_queries()
        d = await describe(conn, sql)
        assert len(d.columns) == 10
        return _catalog_queries() - before

    assert run_checker(schema, body) == 1


def test_no_catalog_round_trip_without_origins(schema: str) -> None:
    async def body(conn) -> int:
        before = _catalog_queries()
        await describe(conn, "SELECT 1")
        return _catalog_queries() - before

    assert run_checker(schema, body) == 0

import random

from rowx.__tests__.nullgen import Col, Join, Shape, generate, minimise, outputs, render


def test_generation_is_deterministic_per_seed() -> None:
    first, again, other = (random.Random(seed) for seed in (11, 11, 12))  # ruff: ignore[suspicious-non-cryptographic-random-usage]
    statements = [generate(first).sql for _ in range(50)]
    assert statements == [generate(again).sql for _ in range(50)]
    assert statements != [generate(other).sql for _ in range(50)]


def test_minimise_drops_everything_the_predicate_does_not_need() -> None:
    shape = Shape(
        base="users",
        joins=(
            Join(kind="LEFT JOIN", source="posts", alias="t1", on="t1.user_id = t0.id"),
            Join(kind="LEFT JOIN", source="orders", alias="t2", on="t2.user_id = t0.id"),
        ),
        columns=(
            Col(expr="t0.name", type="text", name="name"),
            Col(expr="t1.title", type="text", name="title"),
        ),
        where="t0.id IS NOT NULL",
        window="row_number() OVER ()",
        wrap="cte_mat",
    )
    reduced = minimise(shape, lambda sql: "t1.title" in sql and "JOIN posts" in sql and "t2" not in sql)
    assert reduced == "SELECT t1.title AS title FROM users t0 LEFT JOIN posts t1 ON t1.user_id = t0.id"


def test_outputs_name_the_aggregate_and_window_columns() -> None:
    shape = Shape(
        base="users",
        joins=(),
        columns=(Col(expr="t0.id", type="int", name="id"),),
        group="rollup",
        window="row_number() OVER ()",
    )
    assert outputs(shape) == [("id", "int"), ("n", "num"), ("w", "num")]
    assert "GROUP BY ROLLUP(t0.id)" in render(shape)

import pytest

from rowx.__tests__.conformance.row_types import PRELUDE, ROWS
from rowx._rowplan import NO_ROWS, RowPlan, row_plan
from rowx.errors import InterfaceError


def evaluate(source: str) -> object:
    ns: dict[str, object] = {}
    exec(PRELUDE + source, ns)  # noqa: S102
    return ns["T"]


@pytest.mark.parametrize(("source", "expected", "_verdict"), ROWS)
def test_conformance_row(source: str, expected: tuple[str, int, tuple[bool, ...]] | str, _verdict: str) -> None:
    t = evaluate(source)
    if isinstance(expected, str):
        with pytest.raises(InterfaceError):
            row_plan(t)
        return
    kind, arity, mask = expected
    assert row_plan(t) == RowPlan(kind=kind, arity=arity, nullable_mask=mask)  # ty: ignore[invalid-argument-type]


def test_none_is_shared_no_rows_plan() -> None:
    assert row_plan(None) is NO_ROWS


def test_plan_is_cached_per_type() -> None:
    t = evaluate("T = tuple[int, str | None]")
    assert row_plan(t) is row_plan(t)


def test_unhashable_annotation_is_not_cached() -> None:
    t = evaluate("T = Annotated[int, []]")
    assert row_plan(t) == RowPlan(kind="scalar", arity=1, nullable_mask=(False,))


def test_alias_depth_limit() -> None:
    ns: dict[str, object] = {}
    lines = ["type A0 = int"] + [f"type A{i} = A{i - 1}" for i in range(1, 20)]
    exec("\n".join(lines), ns)  # noqa: S102
    with pytest.raises(InterfaceError):
        row_plan(ns["A19"])
    assert row_plan(ns["A10"]).kind == "scalar"

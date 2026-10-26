import pytest

from rowx.__tests__.conformance.row_types import PRELUDE, ROWS
from rowx._rowplan import NO_ROWS, RowPlan, row_plan
from rowx.errors import InterfaceError


def evaluate(source: str) -> object:
    ns: dict[str, object] = {}
    exec(PRELUDE + source, ns)  # ruff: ignore[exec-builtin]
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
    exec("\n".join(lines), ns)  # ruff: ignore[exec-builtin]
    with pytest.raises(InterfaceError):
        row_plan(ns["A19"])
    assert row_plan(ns["A10"]).kind == "scalar"


def test_network_pair_in_either_order_is_one_leaf() -> None:
    forward = evaluate("T = ipaddress.IPv4Network | ipaddress.IPv6Network | None")
    backward = evaluate("T = ipaddress.IPv6Network | ipaddress.IPv4Network")
    assert row_plan(forward) == RowPlan(kind="scalar", arity=1, nullable_mask=(True,))
    assert row_plan(backward) == RowPlan(kind="scalar", arity=1, nullable_mask=(False,))


@pytest.mark.parametrize(
    "source",
    [
        "type V4 = ipaddress.IPv4Interface | None\ntype V6 = ipaddress.IPv6Interface\nT = V4 | V6",
        "type V4 = Annotated[ipaddress.IPv4Interface | None, 1]\nT = Annotated[V4 | ipaddress.IPv6Interface, 2]",
    ],
)
def test_network_pair_split_across_nested_unions_is_one_nullable_leaf(source: str) -> None:
    assert row_plan(evaluate(source)) == RowPlan(kind="scalar", arity=1, nullable_mask=(True,))


def test_cyclic_union_alias_is_rejected() -> None:
    with pytest.raises(InterfaceError):
        row_plan(evaluate("type A = A | int\nT = A"))


@pytest.mark.parametrize("subtype", ["int", "decimal.Decimal", "datetime.date", "datetime.datetime"])
def test_range_is_a_non_nullable_leaf_for_each_subtype(subtype: str) -> None:
    assert row_plan(evaluate(f"T = Range[{subtype}]")) == RowPlan(kind="scalar", arity=1, nullable_mask=(False,))
    assert row_plan(evaluate(f"T = Range[{subtype}] | None")) == RowPlan(kind="scalar", arity=1, nullable_mask=(True,))


def test_range_subtype_is_looked_through_aliases() -> None:
    plan = row_plan(evaluate("type N = Annotated[int, 1]\nT = Range[N]"))
    assert plan == RowPlan(kind="scalar", arity=1, nullable_mask=(False,))


def test_money_and_interval_are_non_nullable_leaves() -> None:
    assert row_plan(evaluate("T = Money")).nullable_mask == (False,)
    assert row_plan(evaluate("T = Interval | None")).nullable_mask == (True,)
    assert row_plan(evaluate("T = list[list[Money]]")).nullable_mask == (False,)


@pytest.mark.parametrize(
    ("source", "spans"),
    [
        ("T = Interval", (True,)),
        ("T = Interval | None", (True,)),
        ("T = list[Interval | None]", (True,)),
        ("T = list[list[Interval]]", (True,)),
        ("T = list[Interval] | None", (True,)),
        ("T = list[list[Interval] | None]", (True,)),
        ("T = list[datetime.timedelta] | None", (False,)),
        ("T = datetime.timedelta", (False,)),
        ("T = Any", (False,)),
        ("T = Money", (False,)),
        ("T = tuple[Interval, datetime.timedelta | None, list[Interval], int]", (True, False, True, False)),
        ("type I = Interval\nT = tuple[I | None, str]", (True, False)),
    ],
)
def test_interval_leaf_is_marked_per_column(source: str, spans: tuple[bool, ...]) -> None:
    assert row_plan(evaluate(source)).interval_mask == spans


def test_flags_pack_nullability_and_the_interval_leaf() -> None:
    plan = row_plan(evaluate("T = tuple[Interval, datetime.timedelta | None, Interval | None, int]"))
    assert plan.flags == [0b10, 0b01, 0b11, 0b00]


def test_interval_and_money_are_rejected_in_unions() -> None:
    for source in ("T = Interval | Money", "T = Interval | datetime.timedelta"):
        with pytest.raises(InterfaceError):
            row_plan(evaluate(source))

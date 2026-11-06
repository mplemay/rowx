import datetime as dt
import importlib
import sys
from decimal import Decimal
from ipaddress import ip_address, ip_network
from typing import Final

import pytest

from rowx.postgres import Box, Circle, Line, LineSegment, Money, Path, Point, Polygon, Range
from rowx.postgres._core import Connection, _param_formats, map_columns


def test_postgres_submodule_imports_with_from_import() -> None:
    assert Connection.__module__ == "rowx.postgres._core"


def test_postgres_submodule_imports_with_importlib() -> None:
    module = importlib.import_module("rowx.postgres._core")
    assert module.Connection.__module__ == "rowx.postgres._core"
    assert module.Connection is Connection


def test_postgres_submodule_is_registered_in_sys_modules() -> None:
    assert sys.modules["rowx.postgres._core"] is importlib.import_module("rowx.postgres._core")


type FieldSpec = tuple[tuple[str, ...] | None, bool]
type Fields = list[FieldSpec]
type Verdict = tuple[
    list[int | None], list[tuple[str, list[int], list[int], str | None, int | None, int | None, int | None]]
]


def req(*names: str) -> FieldSpec:
    return names, True


def opt(*names: str) -> FieldSpec:
    return names, False


def verdict(columns: list[str], fields: Fields, *, positional: bool = False) -> Verdict:
    mapping, issues = map_columns(columns, fields, positional)
    return mapping, [(i.kind, i.columns, i.fields, i.name, i.min, i.max, i.actual) for i in issues]


def issue(
    kind: str,
    *,
    columns: list[int] | None = None,
    fields: list[int] | None = None,
    name: str | None = None,
    arity: tuple[int, int, int] | None = None,
) -> tuple[str, list[int], list[int], str | None, int | None, int | None, int | None]:
    low, high, actual = arity or (None, None, None)
    return kind, columns or [], fields or [], name, low, high, actual


def test_map_columns_by_name_ignores_order_and_strips_overrides() -> None:
    assert verdict(["name?", "id!"], [req("id"), req("name")]) == ([1, 0], [])


def test_map_columns_by_name_is_case_sensitive() -> None:
    assert verdict(["ID"], [req("id")]) == (
        [None],
        [issue("unexpected-column", columns=[0], name="ID"), issue("missing-column", fields=[0])],
    )


def test_map_columns_unexpected_column() -> None:
    assert verdict(["id", "age"], [req("id")]) == (
        [0, None],
        [issue("unexpected-column", columns=[1], name="age")],
    )


def test_map_columns_missing_column_and_absent_default() -> None:
    fields = [req("id"), opt("name")]
    assert verdict(["id"], fields) == ([0], [])
    assert verdict(["name"], fields) == ([1], [issue("missing-column", fields=[0])])


def test_map_columns_duplicate_column_with_a_field() -> None:
    assert verdict(["id", "id!", "id?"], [req("id")]) == (
        [0, 0, 0],
        [issue("duplicate-column", columns=[0, 1, 2], fields=[0], name="id")],
    )


def test_map_columns_duplicate_column_without_a_field_is_not_also_unexpected() -> None:
    assert verdict(["?column?", "id", "?column?"], [req("id")]) == (
        [None, 0, None],
        [issue("duplicate-column", columns=[0, 2], name="?column?")],
    )


def test_map_columns_duplicate_column_for_two_names_of_one_field() -> None:
    fields = [req("user_id", "id")]
    assert verdict(["id", "user_id"], fields) == (
        [0, 0],
        [issue("duplicate-column", columns=[0, 1], fields=[0])],
    )
    assert verdict(["id"], fields) == ([0], [])
    assert verdict(["user_id"], fields) == ([0], [])


def test_map_columns_duplicate_column_keeps_a_partial_mapping() -> None:
    assert verdict(["id", "id"], [req("id")]) == (
        [0, 0],
        [issue("duplicate-column", columns=[0, 1], fields=[0], name="id")],
    )


def test_map_columns_ambiguous_column_keeps_the_other_columns_mapped() -> None:
    assert verdict(["y", "a"], [req("y"), req("y"), req("a")]) == (
        [None, 2],
        [issue("ambiguous-column", columns=[0], fields=[0, 1], name="y")],
    )


def test_map_columns_ambiguous_column() -> None:
    assert verdict(["y"], [req("y"), req("y")]) == (
        [None],
        [issue("ambiguous-column", columns=[0], fields=[0, 1], name="y")],
    )


def test_map_columns_repeated_name_inside_a_field_group_is_one_duplicate() -> None:
    assert verdict(["id", "user_id", "id"], [req("id", "user_id")])[1] == [
        issue("duplicate-column", columns=[0, 1, 2], fields=[0]),
    ]
    assert verdict(["a", "b", "a"], [req("a", "b")])[1] == [
        issue("duplicate-column", columns=[0, 1, 2], fields=[0]),
    ]


def test_map_columns_duplicate_and_ambiguous_are_both_reported() -> None:
    assert verdict(["y", "y"], [req("y"), req("y")])[1] == [
        issue("ambiguous-column", columns=[0], fields=[0, 1], name="y"),
        issue("duplicate-column", columns=[0, 1], fields=[0], name="y"),
        issue("ambiguous-column", columns=[1], fields=[0, 1], name="y"),
    ]


def test_map_columns_unknown_names_let_duplicate_columns_map_to_one_field() -> None:
    assert verdict(["id", "id", "zz"], [req("id"), (None, False)]) == ([0, 0, None], [])


def test_map_columns_unknown_names_pass_columns_through_and_suppress_the_rules() -> None:
    fields: Fields = [req("id"), (None, True)]
    assert verdict(["extra", "extra", "other"], fields) == ([None, None, None], [])
    assert verdict(["id", "x"], fields) == ([0, None], [])


def test_map_columns_positional_accepts_the_arity_range_with_trailing_defaults() -> None:
    fields = [req("id"), req("name"), opt("note")]
    assert verdict(["id", "name"], fields, positional=True) == ([0, 1], [])
    assert verdict(["id", "name", "note"], fields, positional=True) == ([0, 1, 2], [])


def test_map_columns_positional_arity_mismatch_suppresses_name_errors() -> None:
    fields = [req("id"), req("name"), opt("note")]
    assert verdict(["name"], fields, positional=True) == ([None], [issue("arity-mismatch", arity=(2, 3, 1))])
    assert verdict(["a", "b", "c", "d"], fields, positional=True) == (
        [None] * 4,
        [issue("arity-mismatch", arity=(2, 3, 4))],
    )


def test_map_columns_positional_name_mismatch_on_a_swapped_pair() -> None:
    fields = [req("id"), req("name")]
    assert verdict(["name", "id"], fields, positional=True) == (
        [None, None],
        [
            issue("column-name-mismatch", columns=[0], fields=[0], name="name"),
            issue("column-name-mismatch", columns=[1], fields=[1], name="id"),
        ],
    )
    assert verdict(["id!", "name?"], fields, positional=True) == ([0, 1], [])


def test_map_columns_collects_mixed_errors_in_order() -> None:
    fields = [req("a"), req("b", "y"), opt("y")]
    assert verdict(["zzz", "?column?", "y", "?column?", "zzz"], fields)[1] == [
        issue("ambiguous-column", columns=[2], fields=[1, 2], name="y"),
        issue("duplicate-column", columns=[1, 3], name="?column?"),
        issue("duplicate-column", columns=[0, 4], name="zzz"),
        issue("missing-column", fields=[0]),
    ]


def test_map_columns_sorts_by_the_second_column_of_a_duplicate() -> None:
    assert verdict(["q", "a", "q", "w"], [req("a")])[1] == [
        issue("duplicate-column", columns=[0, 2], name="q"),
        issue("unexpected-column", columns=[3], name="w"),
    ]


def test_shape_issue_is_frozen() -> None:
    _, issues = map_columns(["x"], [req("a")], positional=False)
    with pytest.raises(AttributeError):
        issues[0].kind = "other"  # ty: ignore[invalid-assignment]


def point(x: float = 1.0) -> Point:
    return Point(x, 2.0)


# the built-in array type OID of every element family a parameter can name, with a value and a NULL
ARRAY_SAMPLES: Final[dict[str, tuple[int, object]]] = {
    "bool[]": (1000, True),
    "bytea[]": (1001, b"\x00\xff"),
    '"char"[]': (1002, "a"),
    "name[]": (1003, 'a"b'),
    "int2[]": (1005, 7),
    "int4[]": (1007, 7),
    "text[]": (1009, 'a"b\\c,{}NULL ā'),
    "bpchar[]": (1014, "a"),
    "varchar[]": (1015, "a"),
    "int8[]": (1016, 2**62),
    "point[]": (1017, point()),
    "lseg[]": (1018, LineSegment(point(), point(3.0))),
    "path[]": (1019, Path((point(), point(3.0)), closed=False)),
    "box[]": (1020, Box(point(3.0), point())),
    "float4[]": (1021, 1.5),
    "float8[]": (1022, float("nan")),
    "polygon[]": (1027, Polygon((point(), point(3.0), point(5.0)))),
    "oid[]": (1028, 4_000_000_000),
    "macaddr[]": (1040, "08:00:2b:01:02:03"),
    "inet[]": (1041, ip_address("10.0.0.1")),
    "cidr[]": (651, ip_network("10.0.0.0/8")),
    "timestamp[]": (1115, dt.datetime(2024, 1, 2, 3, 4, 5)),  # ruff: ignore[call-datetime-without-tzinfo]
    "date[]": (1182, dt.date(2024, 1, 2)),
    "time[]": (1183, dt.time(1, 2, 3)),
    "timestamptz[]": (1185, dt.datetime(2024, 1, 2, tzinfo=dt.UTC)),
    "interval[]": (1187, dt.timedelta(days=1)),
    "numeric[]": (1231, Decimal("1E+100")),
    "timetz[]": (1270, dt.time(1, 2, 3, tzinfo=dt.UTC)),
    "bit[]": (1561, "101"),
    "varbit[]": (1563, "101"),
    "uuid[]": (2951, "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"),
    "json[]": (199, {"k": "ā"}),
    "jsonb[]": (3807, {"k": "ā"}),
    "money[]": (791, Money(150)),
    "macaddr8[]": (775, "08:00:2b:01:02:03:04:05"),
    "line[]": (629, Line(1.0, 2.0, 3.0)),
    "circle[]": (719, Circle(1.0, 2.0, 3.0)),
    "int4range[]": (3905, Range(1, 5)),
    "numrange[]": (3907, Range(Decimal("1.5"), Decimal("2.5"))),
    "daterange[]": (3913, Range(dt.date(2024, 1, 1), dt.date(2024, 2, 1))),
}


@pytest.mark.parametrize("name", sorted(ARRAY_SAMPLES))
def test_an_array_parameter_is_never_sent_in_text_format(name: str) -> None:
    oid, value = ARRAY_SAMPLES[name]
    # a vector, a NULL-holding vector, a two-dimensional array and an empty one
    arrays: list[object] = [[value], [value, None], [[value, None], [None, value]], []]
    assert _param_formats([oid] * len(arrays), arrays) == ["binary"] * len(arrays)


RANGE_SAMPLES: Final[dict[str, tuple[int, int, object]]] = {
    "int4range": (3904, 3905, 1),
    "int8range": (3926, 3927, 1),
    "numrange": (3906, 3907, Decimal("1.5")),
    "tsrange": (3908, 3909, dt.datetime(2024, 1, 2)),  # ruff: ignore[call-datetime-without-tzinfo]
    "tstzrange": (3910, 3911, dt.datetime(2024, 1, 2, tzinfo=dt.UTC)),
    "daterange": (3912, 3913, dt.date(2024, 1, 2)),
}


@pytest.mark.parametrize("name", sorted(RANGE_SAMPLES))
def test_a_range_parameter_is_never_sent_in_text_format(name: str) -> None:
    scalar_oid, array_oid, bound = RANGE_SAMPLES[name]
    ranges = [
        Range(bound, bound, lower_inc=True, upper_inc=True),
        Range(bound, None),
        Range(None, bound),
        Range(),
        Range(empty=True),
        None,
    ]
    assert _param_formats([scalar_oid] * len(ranges), ranges) == ["binary"] * len(ranges)
    arrays: list[object] = [ranges, [ranges, ranges], []]
    assert _param_formats([array_oid] * len(arrays), arrays) == ["binary"] * len(arrays)


def test_only_scalar_numeric_timetz_and_json_stay_text() -> None:
    zoned = dt.time(1, tzinfo=dt.UTC)
    # numeric and timetz carry text-format scalars; json and jsonb too
    assert _param_formats([1700, 1266, 114, 3802], [Decimal("1.5"), zoned, {"a": 1}, {"a": 1}]) == ["text"] * 4
    assert _param_formats([1700, 1266, 114, 3802], [None] * 4) == ["binary"] * 4

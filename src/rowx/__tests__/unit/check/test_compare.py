from typing import Final

import pytest

from rowx.check.compare import ColumnDesc, DescribeDesc, Finding, compare, compare_param
from rowx.check.params import ParamName, ParamType
from rowx.check.resolve import Elem, RowType
from rowx.postgres._vocab import LeafName, TypeKey


def col(key: str | None, *, nullable: bool = False, name: str = "c", **extra: object) -> ColumnDesc:
    return ColumnDesc(name=name, type_name=key or "money", type_key=key, nullable=nullable, **extra)  # ty: ignore[invalid-argument-type]


def tup(*elems: Elem) -> RowType:
    return RowType(kind="tuple", elements=elems)


def scalar(elem: Elem) -> RowType:
    return RowType(kind="scalar", elements=(elem,))


def run(row_type: RowType, *columns: ColumnDesc) -> list[Finding]:
    return compare(DescribeDesc(columns=columns), row_type)


def rules(findings: list[Finding]) -> list[str]:
    return [finding.rule for finding in findings]


def test_exact_match_is_clean() -> None:
    row = tup(Elem("int", nullable=False), Elem("str", nullable=True))
    assert run(row, col("int4"), col("text", nullable=True)) == []


def test_none_row_type_is_not_compared() -> None:
    assert run(RowType(kind="none"), col("int4")) == []


def test_tuple_arity_mismatch_suppresses_element_rules() -> None:
    findings = run(tup(Elem("str", nullable=False)), col("int4"), col("text"))
    assert rules(findings) == ["arity-mismatch"]


def test_scalar_arity_mismatch() -> None:
    assert rules(run(scalar(Elem("int", nullable=False)), col("int4"), col("int4"))) == ["arity-mismatch"]
    assert rules(run(scalar(Elem("int", nullable=False)))) == ["arity-mismatch"]


def test_unsupported_type() -> None:
    findings = run(scalar(Elem("str", nullable=False)), col(None))
    assert rules(findings) == ["unsupported-type"]


def test_unsupported_type_even_for_any() -> None:
    assert rules(run(scalar(Elem("any", nullable=True)), col(None))) == ["unsupported-type"]


def test_type_mismatch() -> None:
    findings = run(tup(Elem("str", nullable=False)), col("int4"))
    assert rules(findings) == ["type-mismatch"]
    assert findings[0].element == 0
    assert findings[0].column == 0


def test_bool_against_int4_is_type_mismatch() -> None:
    assert rules(run(scalar(Elem("bool", nullable=False)), col("int4"))) == ["type-mismatch"]
    assert rules(run(scalar(Elem("int", nullable=False)), col("bool"))) == ["type-mismatch"]


def test_naive_and_aware_datetimes_are_not_distinguished() -> None:
    elem = Elem("datetime.datetime", nullable=False)
    assert run(scalar(elem), col("timestamp")) == []
    assert run(scalar(elem), col("timestamptz")) == []


def test_type_mismatch_reports_both_kinds_independently() -> None:
    row = tup(Elem("str", nullable=False), Elem("int", nullable=False))
    findings = run(row, col("int4"), col("int4", nullable=True))
    assert [(f.rule, f.element) for f in findings] == [("type-mismatch", 0), ("nullability-mismatch", 1)]


def test_nullable_column_without_none() -> None:
    findings = run(scalar(Elem("str", nullable=False)), col("text", nullable=True, reason="NullableColumn"))
    assert rules(findings) == ["nullability-mismatch"]
    assert findings[0].reason == "NullableColumn"
    assert findings[0].help is not None
    assert '"c!"' in findings[0].help


def test_not_null_column_with_none_is_exact() -> None:
    findings = run(scalar(Elem("str", nullable=True)), col("text"))
    assert rules(findings) == ["nullability-mismatch"]
    assert findings[0].help is not None
    assert '"c?"' in findings[0].help


def test_nullability_carries_reason_detail() -> None:
    column = col("text", nullable=True, reason="OuterJoin", reason_detail="Hash Left Join")
    [finding] = run(scalar(Elem("str", nullable=False)), column)
    assert (finding.reason, finding.reason_detail) == ("OuterJoin", "Hash Left Join")


def test_any_matches_every_type_and_nullability() -> None:
    elem = Elem("any", nullable=True)
    assert run(scalar(elem), col("int4")) == []
    assert run(scalar(elem), col("text", nullable=True)) == []


def test_object_row_element_is_neutral_in_a_tuple() -> None:
    row = tup(Elem("any", nullable=True), Elem("int", nullable=False))
    assert run(row, col("uuid", nullable=True), col("int8")) == []


def test_json_accepts_any_leaf() -> None:
    assert run(scalar(Elem("int", nullable=False)), col("jsonb")) == []
    assert run(scalar(Elem("list", nullable=False, item=Elem("str", nullable=False))), col("json")) == []


def test_json_still_checks_nullability() -> None:
    assert rules(run(scalar(Elem("str", nullable=False)), col("jsonb", nullable=True))) == ["nullability-mismatch"]
    assert rules(run(scalar(Elem("str", nullable=True)), col("jsonb"))) == ["nullability-mismatch"]


def test_json_with_any_is_fully_neutral() -> None:
    assert run(scalar(Elem("any", nullable=True)), col("jsonb")) == []


def test_array_matches_list_of_element() -> None:
    column = col("array", element_key="int4")
    assert run(scalar(Elem("list", nullable=False, item=Elem("int", nullable=False))), column) == []


def test_array_element_may_be_nullable_or_not() -> None:
    column = col("array", element_key="int4")
    assert run(scalar(Elem("list", nullable=False, item=Elem("int", nullable=True))), column) == []


def test_array_nested_levels_accept_optional_elements() -> None:
    inner = Elem("list", nullable=False, item=Elem("int", nullable=True))
    outer = Elem("list", nullable=False, item=inner)
    assert run(scalar(outer), col("array", element_key="int4", dims=2)) == []


def test_array_wrong_item_type() -> None:
    column = col("array", element_key="int4")
    elem = Elem("list", nullable=False, item=Elem("str", nullable=False))
    assert rules(run(scalar(elem), column)) == ["type-mismatch"]


def test_array_depth_mismatch() -> None:
    inner = Elem("list", nullable=False, item=Elem("int", nullable=False))
    column = col("array", element_key="int4")
    assert rules(run(scalar(Elem("list", nullable=False, item=inner)), column)) == ["type-mismatch"]
    assert rules(run(scalar(inner), col("array", element_key="int4", dims=2))) == ["type-mismatch"]


def test_array_column_against_scalar_leaf() -> None:
    assert rules(run(scalar(Elem("int", nullable=False)), col("array", element_key="int4"))) == ["type-mismatch"]


def test_list_against_non_array_column() -> None:
    elem = Elem("list", nullable=False, item=Elem("int", nullable=False))
    assert rules(run(scalar(elem), col("int4"))) == ["type-mismatch"]


def test_array_column_nullability_still_applies() -> None:
    elem = Elem("list", nullable=False, item=Elem("int", nullable=False))
    assert rules(run(scalar(elem), col("array", element_key="int4", nullable=True))) == ["nullability-mismatch"]


def test_array_of_unsupported_element() -> None:
    elem = Elem("list", nullable=False, item=Elem("str", nullable=False))
    assert rules(run(scalar(elem), col("array"))) == ["unsupported-type"]


def test_void_matches_only_none_element() -> None:
    row = tup(Elem("int", nullable=False), Elem("none", nullable=True))
    assert run(row, col("int4"), col("void", nullable=True)) == []


def test_scalar_facing_void_suggests_execute() -> None:
    [finding] = run(scalar(Elem("str", nullable=False)), col("void", nullable=True))
    assert finding.rule == "type-mismatch"
    assert finding.help == "use .execute()"


def test_any_facing_void_is_type_mismatch() -> None:
    assert rules(run(scalar(Elem("any", nullable=True)), col("void"))) == ["type-mismatch"]


def test_none_element_facing_a_real_column_is_type_mismatch() -> None:
    assert rules(run(tup(Elem("none", nullable=True)), col("int4"))) == ["type-mismatch"]


def test_enum_and_domain_columns_use_their_leaf() -> None:
    assert run(scalar(Elem("str", nullable=False)), col("enum")) == []
    assert rules(run(scalar(Elem("int", nullable=False)), col("enum"))) == ["type-mismatch"]


def test_none_element_facing_json_is_type_mismatch() -> None:
    assert rules(run(tup(Elem("none", nullable=True)), col("jsonb", nullable=True))) == ["type-mismatch"]
    assert rules(run(tup(Elem("none", nullable=True)), col("json"))) == ["type-mismatch"]


def test_none_element_facing_array_is_type_mismatch() -> None:
    assert rules(run(tup(Elem("none", nullable=True)), col("array", element_key="int4"))) == ["type-mismatch"]


def test_param_mismatch_names_both_types() -> None:
    finding = compare_param(0, "int4", "int4", ParamType("str"))
    assert finding is not None
    assert finding.rule == "param-type-mismatch"
    assert finding.message == 'parameter $1 has type "int4", which accepts `int`, but the interpolation is `str`'


@pytest.mark.parametrize(
    ("key", "evident"),
    [
        ("int4", None),
        ("int4", ParamType("None")),
        ("numeric", ParamType("int")),
        ("uuid", ParamType("str")),
        ("jsonb", ParamType("Decimal")),
        ("int4", ParamType("int")),
        ("int4[]", None),
        (None, ParamType("str")),
    ],
)
def test_param_not_flagged(key: str | None, evident: ParamType | None) -> None:
    assert compare_param(0, "t", key, evident) is None  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(
    ("key", "evident"),
    [
        ("int4", ParamType("bool")),
        ("text", ParamType("int")),
        ("date", ParamType("datetime")),
        ("bool", ParamType("int")),
    ],
)
def test_param_flagged(key: str, evident: ParamType) -> None:
    assert compare_param(0, key, key, evident) is not None  # ty: ignore[invalid-argument-type]


def test_array_param_checks_elements() -> None:
    assert compare_param(0, "_int4", "array", ParamType("list", ParamType("str")), "int4") is not None
    assert (
        compare_param(0, "_int4", "array", ParamType("list", ParamType("list", ParamType("str"))), "int4") is not None
    )
    assert compare_param(0, "_int4", "array", ParamType("list", ParamType("int")), "int4") is None
    assert compare_param(0, "_uuid", "array", ParamType("list", ParamType("str")), "uuid") is None
    assert compare_param(0, "_int4", "array", ParamType("list"), "int4") is None


RANGE_LEAVES = {
    "int4range": "rowx.postgres.Range[int]",
    "int8range": "rowx.postgres.Range[int]",
    "numrange": "rowx.postgres.Range[decimal.Decimal]",
    "daterange": "rowx.postgres.Range[datetime.date]",
    "tsrange": "rowx.postgres.Range[datetime.datetime]",
    "tstzrange": "rowx.postgres.Range[datetime.datetime]",
}


@pytest.mark.parametrize(("key", "leaf"), RANGE_LEAVES.items())
def test_range_matches_only_its_subtype_leaf(key: str, leaf: str) -> None:
    assert run(scalar(Elem(leaf, nullable=False)), col(key)) == []  # ty: ignore[invalid-argument-type]
    others = {other for other in RANGE_LEAVES.values() if other != leaf}
    for other in others:
        findings = run(scalar(Elem(other, nullable=False)), col(key))  # ty: ignore[invalid-argument-type]
        assert rules(findings) == ["type-mismatch"], other
        assert findings[0].help is not None
        assert key in findings[0].help
        assert leaf in findings[0].help


def test_range_mismatch_help_names_the_subtype() -> None:
    [finding] = run(scalar(Elem("rowx.postgres.Range[decimal.Decimal]", nullable=False)), col("int4range", name="r"))
    assert finding.help == "`int4range` is a range of `int`, so annotate it `rowx.postgres.Range[int]`"


def test_range_array_help_names_the_subtype() -> None:
    column = ColumnDesc(name="c", type_name="_numrange", type_key="array", nullable=False, element_key="numrange")
    elem = Elem("list", nullable=False, item=Elem("rowx.postgres.Range[int]", nullable=False))
    [finding] = run(scalar(elem), column)
    assert finding.rule == "type-mismatch"
    assert finding.help is not None
    assert (
        finding.help
        == "`numrange` is a range of `decimal.Decimal`, so annotate it `rowx.postgres.Range[decimal.Decimal]`"
    )


def test_a_non_range_mismatch_has_no_range_help() -> None:
    [finding] = run(scalar(Elem("str", nullable=False)), col("int4"))
    assert finding.help is None


def test_range_parameter_takes_only_a_range() -> None:
    assert compare_param(0, "int4range", "int4range", ParamType("Range")) is None
    for name in ("int", "str", "list", "Decimal"):
        finding = compare_param(0, "int4range", "int4range", ParamType(name))
        assert finding is not None
        assert finding.rule == "param-type-mismatch"
    assert compare_param(0, "int4", "int4", ParamType("Range")) is not None


def test_range_array_parameter_takes_a_list_of_ranges() -> None:
    ranges = ParamType("list", ParamType("Range"))
    assert compare_param(0, "_int4range", "array", ranges, "int4range") is None
    bad = ParamType("list", ParamType("int"))
    finding = compare_param(0, "_int4range", "array", bad, "int4range")
    assert finding is not None
    assert "whose element" in finding.message


def test_interval_column_matches_either_leaf_and_no_other() -> None:
    assert run(scalar(Elem("datetime.timedelta", nullable=False)), col("interval")) == []
    assert run(scalar(Elem("rowx.postgres.Interval", nullable=False)), col("interval")) == []
    for leaf in ("datetime.time", "rowx.postgres.Money", "int", "str"):
        assert rules(run(scalar(Elem(leaf, nullable=False)), col("interval"))) == ["type-mismatch"], leaf


def test_interval_leaf_matches_no_other_column() -> None:
    interval = Elem("rowx.postgres.Interval", nullable=False)
    for key in ("int4", "timetz", "money", "text", "date"):
        assert rules(run(scalar(interval), col(key))) == ["type-mismatch"], key


def test_interval_array_accepts_either_element_leaf() -> None:
    column = ColumnDesc(name="c", type_name="_interval", type_key="array", nullable=False, element_key="interval")
    for leaf in ("datetime.timedelta", "rowx.postgres.Interval"):
        elem = Elem("list", nullable=False, item=Elem(leaf, nullable=True))
        assert run(scalar(elem), column) == [], leaf
    wrong = Elem("list", nullable=False, item=Elem("rowx.postgres.Money", nullable=False))
    assert rules(run(scalar(wrong), column)) == ["type-mismatch"]


def test_money_matches_only_money() -> None:
    assert run(scalar(Elem("rowx.postgres.Money", nullable=False)), col("money")) == []
    for leaf in ("int", "decimal.Decimal", "str", "float", "rowx.postgres.Interval"):
        assert rules(run(scalar(Elem(leaf, nullable=False)), col("money"))) == ["type-mismatch"], leaf
    [finding] = run(scalar(Elem("decimal.Decimal", nullable=False)), col("money", name="m"))
    assert "`rowx.postgres.Money`" in finding.message


@pytest.mark.parametrize("key", ["bit", "varbit"])
def test_bit_strings_match_only_str(key: str) -> None:
    assert run(scalar(Elem("str", nullable=False)), col(key)) == []
    for leaf in ("int", "bytes", "rowx.postgres.Money"):
        assert rules(run(scalar(Elem(leaf, nullable=False)), col(key))) == ["type-mismatch"], leaf


def test_money_parameter_takes_only_money() -> None:
    assert compare_param(0, "money", "money", ParamType("Money")) is None
    for name in ("int", "str", "float", "Decimal", "Interval"):
        finding = compare_param(0, "money", "money", ParamType(name))
        assert finding is not None, name
        assert finding.rule == "param-type-mismatch"


def test_interval_parameter_takes_interval_or_timedelta() -> None:
    for name in ("Interval", "timedelta"):
        assert compare_param(0, "interval", "interval", ParamType(name)) is None, name
    for name in ("int", "str", "Money", "date"):
        assert compare_param(0, "interval", "interval", ParamType(name)) is not None, name


def test_bit_parameter_takes_only_str() -> None:
    for key in ("bit", "varbit"):
        assert compare_param(0, key, key, ParamType("str")) is None
        for name in ("int", "bytes", "Money"):
            assert compare_param(0, key, key, ParamType(name)) is not None, (key, name)


type GeometricPair = tuple[TypeKey, LeafName, ParamName]

GEOMETRIC: Final[list[GeometricPair]] = [
    ("point", "rowx.postgres.Point", "Point"),
    ("line", "rowx.postgres.Line", "Line"),
    ("lseg", "rowx.postgres.LineSegment", "LineSegment"),
    ("box", "rowx.postgres.Box", "Box"),
    ("path", "rowx.postgres.Path", "Path"),
    ("polygon", "rowx.postgres.Polygon", "Polygon"),
    ("circle", "rowx.postgres.Circle", "Circle"),
]
WRONG_LEAVES: Final[list[LeafName]] = ["str", "float", "int", "rowx.postgres.Money"]
WRONG_PARAMS: Final[list[ParamName]] = ["float", "str", "int", "Money"]


@pytest.mark.parametrize(("key", "leaf", "_name"), GEOMETRIC)
def test_geometric_column_matches_only_its_own_class(key: TypeKey, leaf: LeafName, _name: ParamName) -> None:
    assert run(scalar(Elem(leaf, nullable=False)), col(key)) == []
    for other, other_leaf, _ in GEOMETRIC:
        if other != key:
            found = run(scalar(Elem(other_leaf, nullable=False)), col(key, name="g"))
            assert rules(found) == ["type-mismatch"], other
            assert f"`{leaf}`" in found[0].message
    for wrong in WRONG_LEAVES:
        assert rules(run(scalar(Elem(wrong, nullable=False)), col(key))) == ["type-mismatch"], wrong


@pytest.mark.parametrize(("_key", "leaf", "_name"), GEOMETRIC)
def test_geometric_class_matches_no_other_column(_key: TypeKey, leaf: LeafName, _name: ParamName) -> None:
    for other in ("int4", "text", "money", "interval", "float8"):
        assert rules(run(scalar(Elem(leaf, nullable=False)), col(other))) == ["type-mismatch"], other


@pytest.mark.parametrize(("key", "_leaf", "name"), GEOMETRIC)
def test_geometric_parameter_takes_only_its_own_class(key: TypeKey, _leaf: LeafName, name: ParamName) -> None:
    assert compare_param(0, key, key, ParamType(name)) is None
    for other, _, other_name in GEOMETRIC:
        if other != key:
            finding = compare_param(0, key, key, ParamType(other_name))
            assert finding is not None, (key, other_name)
            assert finding.rule == "param-type-mismatch"
    for wrong in WRONG_PARAMS:
        assert compare_param(0, key, key, ParamType(wrong)) is not None, (key, wrong)


EXTENSION: Final[list[tuple[TypeKey, LeafName, ParamName]]] = [
    ("hstore", "dict[str, str | None]", "dict"),
    ("cube", "rowx.postgres.Cube", "Cube"),
]


@pytest.mark.parametrize(("key", "leaf", "_name"), EXTENSION)
def test_extension_column_matches_only_its_own_leaf(key: TypeKey, leaf: LeafName, _name: ParamName) -> None:
    assert run(scalar(Elem(leaf, nullable=False)), col(key)) == []
    for wrong in ("str", "float", "int", "rowx.postgres.Money"):
        assert rules(run(scalar(Elem(wrong, nullable=False)), col(key))) == ["type-mismatch"], wrong
    for other_key, other_leaf, _ in EXTENSION:
        if other_key != key:
            assert rules(run(scalar(Elem(other_leaf, nullable=False)), col(key))) == ["type-mismatch"], other_key


@pytest.mark.parametrize("key", ["ltree", "lquery"])
def test_label_tree_columns_are_str(key: TypeKey) -> None:
    assert run(scalar(Elem("str", nullable=False)), col(key)) == []
    assert rules(run(scalar(Elem("rowx.postgres.Cube", nullable=False)), col(key))) == ["type-mismatch"]
    assert compare_param(0, key, key, ParamType("str")) is None
    assert compare_param(0, key, key, ParamType("dict")) is not None


@pytest.mark.parametrize(("key", "_leaf", "name"), EXTENSION)
def test_extension_parameter_takes_only_its_own_class(key: TypeKey, _leaf: LeafName, name: ParamName) -> None:
    assert compare_param(0, key, key, ParamType(name)) is None
    for wrong in ("str", "int", "float", "list", "Money"):
        assert compare_param(0, key, key, ParamType(wrong)) is not None, (key, wrong)

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from rowx_postgres._backend import load
from rowx_postgres._vocab import LeafName, TypeKey

from rowx.check.compare import ColumnDesc, DescribeDesc, Finding, compare, compare_param, compare_param_result
from rowx.check.model import Span
from rowx.check.params import ParamName, ParamType
from rowx.check.resolve import Elem, NamedField, RowType

if TYPE_CHECKING:
    from rowx_core.backend import CheckerBackend

PG_RULES: Final[CheckerBackend] = load().checker()


def col(key: str | None, *, nullable: bool = False, name: str = "c", **extra: object) -> ColumnDesc:
    return ColumnDesc(name=name, type_name=key or "money", type_key=key, nullable=nullable, **extra)  # ty: ignore[invalid-argument-type]


def tup(*elems: Elem) -> RowType:
    return RowType(kind="tuple", elements=elems)


def scalar(elem: Elem) -> RowType:
    return RowType(kind="scalar", elements=(elem,))


def run(row_type: RowType, *columns: ColumnDesc) -> list[Finding]:
    return compare(DescribeDesc(columns=columns), row_type, backend=PG_RULES)


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
    assert run(scalar(outer), col("array", element_key="int4")) == []


def test_array_wrong_item_type() -> None:
    column = col("array", element_key="int4")
    elem = Elem("list", nullable=False, item=Elem("str", nullable=False))
    assert rules(run(scalar(elem), column)) == ["type-mismatch"]


def test_array_accepts_any_nesting_depth() -> None:
    column = col("array", element_key="int4")
    elem = Elem("int", nullable=False)
    for depth in range(1, 4):
        nested = elem
        for _ in range(depth):
            nested = Elem("list", nullable=False, item=nested)
        assert run(scalar(nested), column) == []


def test_array_nested_wrong_leaf_is_mismatch() -> None:
    inner = Elem("list", nullable=False, item=Elem("str", nullable=False))
    elem = Elem("list", nullable=False, item=inner)
    assert rules(run(scalar(elem), col("array", element_key="int4"))) == ["type-mismatch"]


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
    finding = compare_param(0, "int4", "int4", ParamType("str"), backend=PG_RULES)
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
        ("time", ParamType("time")),
        ("timetz", ParamType("time")),
        ("bytea", ParamType("bytearray")),
        ("bytea", ParamType("memoryview")),
    ],
)
def test_param_not_flagged(key: str | None, evident: ParamType | None) -> None:
    assert compare_param(0, "t", key, evident, backend=PG_RULES) is None


def test_null_parameter_comparison_coverage() -> None:
    null = ParamType("None")
    nulls = ParamType("list", null)
    assert compare_param_result(0, "int4", "int4", null, backend=PG_RULES) == (None, True)
    assert compare_param_result(0, "int4[]", "array", nulls, "int4", PG_RULES) == (None, True)
    assert compare_param_result(0, "int4", "int4", None, backend=PG_RULES) == (None, False)
    assert compare_param_result(0, "unknown", None, null, backend=PG_RULES) == (None, False)
    assert compare_param_result(0, "unknown[]", "array", nulls, None, PG_RULES) == (None, False)


@pytest.mark.parametrize(
    ("key", "evident"),
    [
        ("int4", ParamType("bool")),
        ("text", ParamType("int")),
        ("date", ParamType("datetime")),
        ("bool", ParamType("int")),
        ("int4", ParamType("time")),
        ("int4", ParamType("bytearray")),
        ("int4", ParamType("memoryview")),
        ("text", ParamType("memoryview")),
    ],
)
def test_param_flagged(key: str, evident: ParamType) -> None:
    assert compare_param(0, key, key, evident, backend=PG_RULES) is not None


def test_array_param_checks_elements() -> None:
    assert compare_param(0, "_int4", "array", ParamType("list", ParamType("str")), "int4", backend=PG_RULES) is not None
    assert (
        compare_param(
            0, "_int4", "array", ParamType("list", ParamType("list", ParamType("str"))), "int4", backend=PG_RULES
        )
        is not None
    )
    assert compare_param(0, "_int4", "array", ParamType("list", ParamType("int")), "int4", backend=PG_RULES) is None
    assert compare_param(0, "_uuid", "array", ParamType("list", ParamType("str")), "uuid", backend=PG_RULES) is None
    assert compare_param(0, "_int4", "array", ParamType("list"), "int4", backend=PG_RULES) is None


RANGE_LEAVES: Final[dict[str, str]] = {
    "int4range": "rowx_postgres.Range[int]",
    "int8range": "rowx_postgres.Range[int]",
    "numrange": "rowx_postgres.Range[decimal.Decimal]",
    "daterange": "rowx_postgres.Range[datetime.date]",
    "tsrange": "rowx_postgres.Range[datetime.datetime]",
    "tstzrange": "rowx_postgres.Range[datetime.datetime]",
}


@pytest.mark.parametrize(("key", "leaf"), RANGE_LEAVES.items())
def test_range_matches_only_its_subtype_leaf(key: str, leaf: str) -> None:
    assert run(scalar(Elem(leaf, nullable=False)), col(key)) == []
    others = {other for other in RANGE_LEAVES.values() if other != leaf}
    for other in others:
        findings = run(scalar(Elem(other, nullable=False)), col(key))
        assert rules(findings) == ["type-mismatch"], other
        assert findings[0].help is not None
        assert key in findings[0].help
        assert leaf in findings[0].help


def test_range_mismatch_help_names_the_subtype() -> None:
    [finding] = run(scalar(Elem("rowx_postgres.Range[decimal.Decimal]", nullable=False)), col("int4range", name="r"))
    assert finding.help == "`int4range` is a range of `int`, so annotate it `rowx_postgres.Range[int]`"


def test_range_array_help_names_the_subtype() -> None:
    column = ColumnDesc(name="c", type_name="_numrange", type_key="array", nullable=False, element_key="numrange")
    elem = Elem("list", nullable=False, item=Elem("rowx_postgres.Range[int]", nullable=False))
    [finding] = run(scalar(elem), column)
    assert finding.rule == "type-mismatch"
    assert finding.help is not None
    assert (
        finding.help
        == "`numrange` is a range of `decimal.Decimal`, so annotate it `rowx_postgres.Range[decimal.Decimal]`"
    )


def test_a_non_range_mismatch_has_no_range_help() -> None:
    [finding] = run(scalar(Elem("str", nullable=False)), col("int4"))
    assert finding.help is None


def test_range_parameter_takes_only_a_range() -> None:
    assert compare_param(0, "int4range", "int4range", ParamType("Range"), backend=PG_RULES) is None
    for name in ("int", "str", "list", "Decimal"):
        finding = compare_param(0, "int4range", "int4range", ParamType(name), backend=PG_RULES)
        assert finding is not None
        assert finding.rule == "param-type-mismatch"
    assert compare_param(0, "int4", "int4", ParamType("Range"), backend=PG_RULES) is not None


def test_range_array_parameter_takes_a_list_of_ranges() -> None:
    ranges = ParamType("list", ParamType("Range"))
    assert compare_param(0, "_int4range", "array", ranges, "int4range", backend=PG_RULES) is None
    bad = ParamType("list", ParamType("int"))
    finding = compare_param(0, "_int4range", "array", bad, "int4range", backend=PG_RULES)
    assert finding is not None
    assert "whose element" in finding.message


def test_interval_column_matches_either_leaf_and_no_other() -> None:
    assert run(scalar(Elem("datetime.timedelta", nullable=False)), col("interval")) == []
    assert run(scalar(Elem("rowx_postgres.Interval", nullable=False)), col("interval")) == []
    for leaf in ("datetime.time", "rowx_postgres.Money", "int", "str"):
        assert rules(run(scalar(Elem(leaf, nullable=False)), col("interval"))) == ["type-mismatch"], leaf


def test_interval_leaf_matches_no_other_column() -> None:
    interval = Elem("rowx_postgres.Interval", nullable=False)
    for key in ("int4", "timetz", "money", "text", "date"):
        assert rules(run(scalar(interval), col(key))) == ["type-mismatch"], key


def test_interval_array_accepts_either_element_leaf() -> None:
    column = ColumnDesc(name="c", type_name="_interval", type_key="array", nullable=False, element_key="interval")
    for leaf in ("datetime.timedelta", "rowx_postgres.Interval"):
        elem = Elem("list", nullable=False, item=Elem(leaf, nullable=True))
        assert run(scalar(elem), column) == [], leaf
    wrong = Elem("list", nullable=False, item=Elem("rowx_postgres.Money", nullable=False))
    assert rules(run(scalar(wrong), column)) == ["type-mismatch"]


def test_money_matches_only_money() -> None:
    assert run(scalar(Elem("rowx_postgres.Money", nullable=False)), col("money")) == []
    for leaf in ("int", "decimal.Decimal", "str", "float", "rowx_postgres.Interval"):
        assert rules(run(scalar(Elem(leaf, nullable=False)), col("money"))) == ["type-mismatch"], leaf
    [finding] = run(scalar(Elem("decimal.Decimal", nullable=False)), col("money", name="m"))
    assert "`rowx_postgres.Money`" in finding.message


@pytest.mark.parametrize("key", ["bit", "varbit"])
def test_bit_strings_match_only_str(key: str) -> None:
    assert run(scalar(Elem("str", nullable=False)), col(key)) == []
    for leaf in ("int", "bytes", "rowx_postgres.Money"):
        assert rules(run(scalar(Elem(leaf, nullable=False)), col(key))) == ["type-mismatch"], leaf


def test_money_parameter_takes_only_money() -> None:
    assert compare_param(0, "money", "money", ParamType("Money"), backend=PG_RULES) is None
    for name in ("int", "str", "float", "Decimal", "Interval"):
        finding = compare_param(0, "money", "money", ParamType(name), backend=PG_RULES)
        assert finding is not None, name
        assert finding.rule == "param-type-mismatch"


def test_interval_parameter_takes_interval_or_timedelta() -> None:
    for name in ("Interval", "timedelta"):
        assert compare_param(0, "interval", "interval", ParamType(name), backend=PG_RULES) is None, name
    for name in ("int", "str", "Money", "date"):
        assert compare_param(0, "interval", "interval", ParamType(name), backend=PG_RULES) is not None, name


def test_bit_parameter_takes_only_str() -> None:
    for key in ("bit", "varbit"):
        assert compare_param(0, key, key, ParamType("str"), backend=PG_RULES) is None
        for name in ("int", "bytes", "Money"):
            assert compare_param(0, key, key, ParamType(name), backend=PG_RULES) is not None, (key, name)


type GeometricPair = tuple[TypeKey, LeafName, ParamName]

GEOMETRIC: Final[list[GeometricPair]] = [
    ("point", "rowx_postgres.Point", "Point"),
    ("line", "rowx_postgres.Line", "Line"),
    ("lseg", "rowx_postgres.LineSegment", "LineSegment"),
    ("box", "rowx_postgres.Box", "Box"),
    ("path", "rowx_postgres.Path", "Path"),
    ("polygon", "rowx_postgres.Polygon", "Polygon"),
    ("circle", "rowx_postgres.Circle", "Circle"),
]
WRONG_LEAVES: Final[list[LeafName]] = ["str", "float", "int", "rowx_postgres.Money"]
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
    assert compare_param(0, key, key, ParamType(name), backend=PG_RULES) is None
    for other, _, other_name in GEOMETRIC:
        if other != key:
            finding = compare_param(0, key, key, ParamType(other_name), backend=PG_RULES)
            assert finding is not None, (key, other_name)
            assert finding.rule == "param-type-mismatch"
    for wrong in WRONG_PARAMS:
        assert compare_param(0, key, key, ParamType(wrong), backend=PG_RULES) is not None, (key, wrong)


EXTENSION: Final[list[tuple[TypeKey, LeafName, ParamName]]] = [
    ("hstore", "dict[str, str | None]", "dict"),
    ("cube", "rowx_postgres.Cube", "Cube"),
]


@pytest.mark.parametrize(("key", "leaf", "_name"), EXTENSION)
def test_extension_column_matches_only_its_own_leaf(key: TypeKey, leaf: LeafName, _name: ParamName) -> None:
    assert run(scalar(Elem(leaf, nullable=False)), col(key)) == []
    for wrong in ("str", "float", "int", "rowx_postgres.Money"):
        assert rules(run(scalar(Elem(wrong, nullable=False)), col(key))) == ["type-mismatch"], wrong
    for other_key, other_leaf, _ in EXTENSION:
        if other_key != key:
            assert rules(run(scalar(Elem(other_leaf, nullable=False)), col(key))) == ["type-mismatch"], other_key


@pytest.mark.parametrize("key", ["ltree", "lquery"])
def test_label_tree_columns_are_str(key: TypeKey) -> None:
    assert run(scalar(Elem("str", nullable=False)), col(key)) == []
    assert rules(run(scalar(Elem("rowx_postgres.Cube", nullable=False)), col(key))) == ["type-mismatch"]
    assert compare_param(0, key, key, ParamType("str"), backend=PG_RULES) is None
    assert compare_param(0, key, key, ParamType("dict"), backend=PG_RULES) is not None


@pytest.mark.parametrize(("key", "_leaf", "name"), EXTENSION)
def test_extension_parameter_takes_only_its_own_class(key: TypeKey, _leaf: LeafName, name: ParamName) -> None:
    assert compare_param(0, key, key, ParamType(name), backend=PG_RULES) is None
    for wrong in ("str", "int", "float", "list", "Money"):
        assert compare_param(0, key, key, ParamType(wrong), backend=PG_RULES) is not None, (key, wrong)


def field(
    key: str,
    leaf: LeafName = "int",
    *,
    nullable: bool = False,
    required: bool = True,
    # an empty tuple means the field's own name
    names: tuple[str, ...] | None = (),
) -> NamedField:
    return NamedField(
        key=key,
        names=names if names != () else (key,),
        elem=Elem(leaf, nullable=nullable),
        required=required,
        path=Path("models.py"),
        span=Span(line=3, col=5, end_line=3, end_col=10),
    )


def unknown(key: str, reason: str, leaf: LeafName = "int") -> NamedField:
    return replace(field(key, leaf, names=None), reason=reason)


def named(*fields: NamedField, form: str = "keyword", matching: str = "by_name") -> RowType:
    return RowType(kind="named", form=form, matching=matching, type_name="M", fields=fields)  # ty: ignore[invalid-argument-type]


def column(name: str, key: str = "int4", *, raw: str | None = None, nullable: bool = False) -> ColumnDesc:
    return col(key, name=name, nullable=nullable, raw_name=raw)


def test_a_named_row_maps_by_name_in_any_order() -> None:
    row = named(field("id"), field("name", "str", nullable=True))
    assert run(row, column("name", "text", nullable=True), column("id")) == []


def test_the_raw_names_are_stripped_once_by_map_columns() -> None:
    row = named(field("id"), field("total"))
    assert run(row, column("id", raw="id!"), column("total", raw="total?")) == []
    # `id!!` is `id!` after one strip, which no field has
    findings = run(named(field("id")), column("id!", raw="id!!"))
    assert rules(findings) == ["unexpected-column", "missing-column"]


def test_the_by_name_rules_and_their_messages() -> None:
    row = named(field("id"), field("name", "str"), field("opt", required=False))
    findings = run(row, column("id"), column("age"), column("id"), column("?column?"), column("?column?"))
    assert [(f.rule, f.column, f.fields) for f in findings] == [
        ("unexpected-column", 1, ()),
        ("duplicate-column", 2, ()),
        ("duplicate-column", 4, ()),
        ("missing-column", None, (1,)),
    ]
    by_rule = {f.rule: f for f in findings}
    assert by_rule["missing-column"].message == 'field "name" of `M` has no column'
    assert by_rule["missing-column"].help == (
        'select a column named "name", alias one with AS name, or give the field a default'
    )
    assert by_rule["unexpected-column"].message == 'column 2 "age" matches no field of `M`'
    assert by_rule["unexpected-column"].help == 'add a field named "age" to `M`, or remove the column from the SELECT'


def test_findings_come_by_column_then_missing_columns_by_field() -> None:
    row = named(field("a"), field("b"), field("c", "str"))
    findings = run(row, column("zzz"), column("a", "text"), column("yyy"))
    assert [(f.rule, f.column, f.fields) for f in findings] == [
        ("unexpected-column", 0, ()),
        ("type-mismatch", 1, (0,)),
        ("unexpected-column", 2, ()),
        ("missing-column", None, (1,)),
        ("missing-column", None, (2,)),
    ]


def test_duplicate_columns_are_not_also_unexpected_or_type_checked() -> None:
    row = named(field("id", "str"))
    findings = run(row, column("id"), column("id"), column("zzz"))
    assert [(f.rule, f.column) for f in findings] == [("duplicate-column", 1), ("unexpected-column", 2)]
    assert findings[0].message == 'columns 1 and 2 are both named "id"'
    assert findings[0].help == "alias one of the columns with AS"
    triple = run(named(field("x")), column("id"), column("id"), column("id"), column("x"))
    assert triple[0].message == 'columns 1, 2 and 3 are all named "id"'


def test_an_unnamed_duplicate_says_so() -> None:
    findings = run(named(field("a", required=False)), column("?column?"), column("?column?"))
    assert [(f.rule, f.column, f.help) for f in findings] == [("duplicate-column", 1, "alias the expression")]
    assert findings[0].message == "columns 1 and 2 are both unnamed (`?column?`)"


def test_per_field_rules_run_on_cleanly_mapped_columns_after_by_name_errors() -> None:
    row = named(field("id", "str"), field("missing"))
    findings = run(row, column("id"), column("extra"))
    assert rules(findings) == ["type-mismatch", "unexpected-column", "missing-column"]
    assert findings[0].message == 'column 1 "id" is `int` but field "id" of `M` is `str`'
    assert findings[0].fields == (0,)


def test_a_nullability_mismatch_names_the_field() -> None:
    row = named(field("email", "str"))
    [finding] = run(row, column("email", "text", nullable=True))
    assert finding.message == 'column 1 "email" may be NULL but field "email" of `M` is `str`'
    assert finding.fields == (0,)
    [finding] = run(named(field("email", "str", nullable=True)), column("email", "text"))
    assert finding.message == 'column 1 "email" is NOT NULL but field "email" of `M` is `str | None`'


def test_an_ambiguous_column_names_each_field_and_is_skipped_by_the_type_rules() -> None:
    row = named(field("x", "str", names=("y",)), field("y", "str"))
    [finding] = run(row, column("y"))
    assert (finding.rule, finding.column, finding.fields) == ("ambiguous-column", 0, (0, 1))
    assert finding.message == 'column 1 "y" matches fields "x" and "y" of `M`'
    assert finding.help == "give the fields distinct aliases, or remove populate_by_name if it causes the overlap"


def test_two_names_of_one_field_are_a_duplicate_with_that_field() -> None:
    row = named(field("id", names=("id", "user_id")))
    [finding] = run(row, column("id"), column("user_id"))
    assert (finding.rule, finding.column, finding.fields) == ("duplicate-column", 1, (0,))
    assert finding.message == 'columns 1 "id" and 2 "user_id" both map to field "id" of `M`'
    assert finding.help == 'select only one of "id" and "user_id"'


def test_unknown_names_skip_the_by_name_rules_but_not_the_type_rules() -> None:
    row = named(field("id", "str"), field("other", names=None))
    findings = run(row, column("id"), column("anything"))
    assert rules(findings) == ["type-mismatch"]


def test_the_missing_column_help_for_a_typeddict_says_notrequired() -> None:
    row = named(field("id"), field("n", "str"), form="dict")
    [finding] = run(row, column("id"))
    assert finding.help == 'select a column named "n", alias one with AS n, or mark the key `NotRequired`'
    missing = next(f for f in run(named(field("Name", "str")), column("id")) if f.rule == "missing-column")
    assert 'alias one with AS "Name"' in (missing.help or "")


def test_a_positional_row_checks_arity_and_names_and_then_stops() -> None:
    row = named(
        field("id"),
        field("name", "str"),
        field("note", "str", required=False),
        form="positional",
        matching="positional",
    )
    assert run(row, column("id"), column("name", "text")) == []
    findings = run(row, column("name", "text"), column("id", "int4"))
    assert [(f.rule, f.column, f.fields) for f in findings] == [
        ("column-name-mismatch", 0, (0,)),
        ("column-name-mismatch", 1, (1,)),
    ]
    assert findings[0].message == 'column 1 "name" faces field "id" of `M` (position 1)'
    assert findings[0].help == "reorder the SELECT, alias the column AS id, or rename the field"
    [finding] = run(row, column("id"))
    assert finding.rule == "arity-mismatch"
    assert finding.message == "the query returns 1 columns but `M` takes between 2 and 3"
    assert finding.help == "select between 2 and 3 columns"
    [finding] = run(named(field("id"), form="positional", matching="positional"), column("id"), column("x"))
    assert finding.message == "the query returns 2 columns but `M` takes exactly 1"
    assert finding.help == "select exactly 1 columns"
    # a type mismatch is not reported next to a name error
    assert rules(run(row, column("name", "int4"), column("name", "text"))) == ["column-name-mismatch"]


def test_a_void_column_faces_a_field_typed_none() -> None:
    row = named(field("v", "none", nullable=True), field("id"))
    assert run(row, column("v", "void"), column("id")) == []


def test_unknown_names_skip_the_by_name_rules_and_keep_the_element_rules() -> None:
    row = named(
        unknown("a", "field `a` uses AliasChoices"),
        field("b", "str", names=("bb",), required=True),
        form="validator",
    )
    # `x` and the second `bb` would be unexpected and duplicate, and `a` would be missing, were its names known
    findings = run(row, column("bb", "int4"), column("x"), column("bb", "text"))
    assert [(f.rule, f.column) for f in findings] == [("type-mismatch", 0)]
    assert findings[0].notes == ("names not checked: field `a` uses AliasChoices",)
    # a clean query has no diagnostic to carry the note, and `b` is still found by its known name
    assert run(row, column("bb", "text"), column("anything")) == []


def test_the_reasons_of_every_unknown_field_are_listed_once() -> None:
    row = named(
        unknown("a", "field `a` uses AliasChoices"),
        unknown("b", "field `b` uses AliasPath"),
        unknown("c", "field `b` uses AliasPath"),
        field("d", "str"),
        form="validator",
    )
    [finding] = run(row, column("d", "int4"), column("x", "text"))
    assert finding.notes == ("names not checked: field `a` uses AliasChoices; field `b` uses AliasPath",)
    # when every field is unknown no column maps to one, so nothing is reported
    row = named(unknown("a", "`M` has a before-validator"), form="validator")
    assert run(row, column("a", "text")) == []


def test_a_model_reason_is_noted_on_the_diagnostics_that_are_emitted() -> None:
    reason = "`M` has a before-validator"
    # with the names of every field unknown, no column maps to a field, so there is no diagnostic to carry the note
    row = named(unknown("a", reason), unknown("b", reason, "str"), form="validator")
    assert run(row, column("a", "text"), column("b", "int4")) == []
    # a known field beside them is compared, and its diagnostic says what was not checked, once
    row = named(unknown("a", reason), unknown("b", reason), field("c", "str"), form="validator")
    [finding] = run(row, column("c", "int4"), column("a"), column("b"))
    assert finding.rule == "type-mismatch"
    assert finding.notes == (f"names not checked: {reason}",)


def test_known_names_carry_no_note() -> None:
    row = named(field("id", "str"), form="validator")
    [finding] = run(row, column("id", "int4"))
    assert finding.rule == "type-mismatch"
    assert finding.notes == ()


def test_a_pydantic_alias_equal_to_another_fields_name_is_ambiguous() -> None:
    row = named(field("x", names=("y",)), field("y"), form="validator")
    findings = run(row, column("y"))
    assert [(f.rule, f.column, f.fields) for f in findings] == [("ambiguous-column", 0, (0, 1))]
    assert findings[0].message == 'column 1 "y" matches fields "x" and "y" of `M`'


def test_both_names_of_one_pydantic_field_are_a_duplicate() -> None:
    row = named(field("id", names=("user_id", "id")), form="validator")
    findings = run(row, column("id"), column("user_id"))
    assert [(f.rule, f.column) for f in findings] == [("duplicate-column", 1)]
    assert findings[0].help == 'select only one of "id" and "user_id"'
    assert rules(run(row, column("user_id"))) == []
    assert rules(run(row, column("id"))) == []

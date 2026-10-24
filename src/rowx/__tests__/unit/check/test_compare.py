from rowx.check.compare import ColumnDesc, DescribeDesc, Finding, compare
from rowx.check.resolve import Elem, RowType


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

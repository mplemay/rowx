import importlib
import sys

import pytest

from rowx._core.postgres import Connection, map_columns


def test_postgres_submodule_imports_with_from_import() -> None:
    assert Connection.__module__ == "rowx._core.postgres"


def test_postgres_submodule_imports_with_importlib() -> None:
    module = importlib.import_module("rowx._core.postgres")
    assert module.Connection.__module__ == "rowx._core.postgres"
    assert module.Connection is Connection


def test_postgres_submodule_is_registered_in_sys_modules() -> None:
    assert sys.modules["rowx._core.postgres"] is importlib.import_module("rowx._core.postgres")


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

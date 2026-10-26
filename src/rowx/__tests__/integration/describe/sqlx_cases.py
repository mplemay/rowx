# Ported from sqlx (https://github.com/launchbadge/sqlx), tests/postgres/describe.rs and macros.rs,
# which is dual-licensed MIT OR Apache-2.0. Copyright (C) SQLx Contributors; portions copyright
# (C) LaunchBadge, LLC. Expectations are rowx's: (name, nullable, reason).
from typing import Final

type Case = tuple[str, list[tuple[str, bool, str]]]

SQLX_CASES: Final[dict[str, Case]] = {
    # it_describes_simple: plain columns of a table keep their catalog nullability
    "simple": (
        "SELECT id, name, email FROM users",
        [("id", False, "NotNullColumn"), ("name", False, "NotNullColumn"), ("email", True, "NullableColumn")],
    ),
    # it_describes_expression: postgres cannot infer nullability of an expression
    "expression": ("SELECT 1::int8 + 10", [("?column?", True, "Computed")]),
    # test_column_override_nullable: a NOT NULL column of a left-joined table is declared nullable
    "override_question": (
        'SELECT name AS "name?" FROM (VALUES (1)) foo(id) LEFT JOIN users ON false',
        [("name", True, "OverrideQuestion")],
    ),
    # query macros: `"col!"` forces NOT NULL
    "override_bang": (
        'SELECT id AS "id!", name AS "name!" FROM (VALUES (1, NULL::text)) accounts(id, name)',
        [("id", False, "OverrideBang"), ("name", False, "OverrideBang")],
    ),
    "override_literal": ('SELECT 1 AS "foo!"', [("foo", False, "OverrideBang")]),
    # sqlx#367: a nullable-by-join column without an override stays nullable
    "left_join_false": (
        "SELECT name FROM (VALUES (1)) foo(id) LEFT JOIN users ON false",
        [("name", True, "OuterJoin")],
    ),
    # it_describes_enum: an enum literal is a computed column
    "enum": ("SELECT 'ok'::mood AS _1", [("_1", True, "Computed")]),
    # it_describes_record: a row constructor is a computed column named `row`
    "record": ("SELECT (true, 10::int2)", [("row", True, "Computed")]),
    # test_void: a function returning void is a computed column
    "void": ("SELECT pg_notify('chan', 'message')", [("pg_notify", True, "Computed")]),
    # test_call_procedure: CALL returns the INOUT parameters, and EXPLAIN cannot plan it
    "call_procedure": ("CALL forty_two(NULL)", [("answer", True, "ExplainFailed")]),
    # test_query_scalar: `?` and `!` on a literal
    "override_question_literal": ('SELECT 1 AS "foo?"', [("foo", True, "OverrideQuestion")]),
    # test_column_override_not_null: `!` on a literal
    "override_bang_literal": ('SELECT 1 AS "id!"', [("id", False, "OverrideBang")]),
    # test_query_as: `!` on a VALUES column, the other column is computed
    "query_as_bang": (
        "SELECT id \"id!\", name FROM (VALUES (1, 'x')) accounts(id, name)",
        [("id", False, "OverrideBang"), ("name", True, "Computed")],
    ),
    # test_query_as_raw: `!` on a reserved-word column name
    "query_as_raw_bang": (
        'SELECT type "type!", name FROM (VALUES (1, NULL::text)) accounts(type, name)',
        [("type", False, "OverrideBang"), ("name", True, "Computed")],
    ),
    # test_non_null: a table column keeps its catalog nullability under LIMIT
    "non_null_limit": (
        "SELECT id, name, email FROM users LIMIT 1",
        [("id", False, "NotNullColumn"), ("name", False, "NotNullColumn"), ("email", True, "NullableColumn")],
    ),
    # test_text_var_char_char_n: casts are computed columns
    "text_cast": ("SELECT 'Hello'::text AS greeting", [("greeting", True, "Computed")]),
    "varchar_cast": ("SELECT 'Hello'::varchar(5) AS greeting", [("greeting", True, "Computed")]),
    "char_cast": ("SELECT 'Hello'::char(5) AS greeting", [("greeting", True, "Computed")]),
    # test_array_from_slice: an array parameter cast and an array constructor
    "array_param": ("SELECT $1::int[] AS my_array", [("my_array", True, "Computed")]),
    "array_constructor": ("SELECT ARRAY[4,3,2,1] AS my_array", [("my_array", True, "Computed")]),
    # fetch_is_usable_issue_224: a set-returning function column has no table origin
    "generate_series": ("SELECT * FROM generate_series(1, 3) AS series(num)", [("num", True, "Computed")]),
    # test_column_override_wildcard, _exact: `: Type` is a sqlx type override with no meaning in rowx, so it is
    # not stripped and the verdict is the catalog's
    "type_override_wildcard": ('SELECT id AS "id: _" FROM users', [("id: _", False, "NotNullColumn")]),
    "type_override_exact": ('SELECT email AS "id: MyInt" FROM users', [("id: MyInt", True, "NullableColumn")]),
    # test_column_override_wildcard_not_null, _nullable: `!: _` and `?: _` are not overrides in rowx either
    "type_override_bang_wildcard": ('SELECT email AS "id!: _" FROM users', [("id!: _", True, "NullableColumn")]),
    "type_override_question_wildcard": ('SELECT id AS "id?: _" FROM users', [("id?: _", False, "NotNullColumn")]),
}

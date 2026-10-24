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
}

# Known limits

Nullability inference limits from [the design](design/nullability.md#known-limits-pinned-by-tests-documented), each
pinned by a test in `src/rowx/__tests__/integration/describe/test_known_limits.py`. A `limit` is a case where rowx is
less precise than the database allows. A `correct` case is a hard-looking shape that rowx gets right.

| Kind | Test | Behaviour |
|---|---|---|
| correct | `test_lateral_on_outer_join` | A LATERAL subquery on the null-extended side is nullable (`OuterJoin`). |
| correct | `test_window_over_outer_join` | A window function over an outer join, behind a subquery, is nullable (`ScanBoundary`). |
| correct | `test_nested_cte_chain` | A nested CTE chain over an outer join is nullable (`ScanBoundary`). |
| correct | `test_scan_boundary_over_promotes` | A scan boundary column from the non-null side stays NOT NULL. Computed, duplicate-named, renamed (swapped-alias) and constant outputs, and columns that merge inputs (`UNION ALL`), stay nullable (`ScanBoundary`). |
| correct | `test_view_columns_are_nullable` | A view column is NOT NULL (`ViewColumn`) when the view's definition proves it, such as a column from the non-null side of its join. Columns on the nullable side, computed columns and views that fall back (extra rules, unreadable `security_invoker` views, chains deeper than 8) stay nullable (`ForeignOrView`). |
| correct | `test_set_operation_not_null_is_inferred` | A set operation column is NOT NULL (`SetOperation`) when the plan proves that output position, through `UNION`, `UNION ALL`, `EXCEPT` and `INTERSECT`, nested or mixed. |
| correct | `test_set_operation_stays_nullable_when_unproven` | A set operation column stays nullable when any branch is unproven: a nullable or `NOT VALID` column, a `NULL` or computed branch, a null-extended join side, or a position that only matches by name. |
| limit | `test_recursive_cte_is_nullable` | Recursive CTE columns have no table origin, so they default to nullable. |
| limit | `test_computed_column_is_nullable` | A computed column outside the whitelist (`upper(name)`, `id + 1`) is nullable (`Computed`), even when its inputs are NOT NULL. |
| correct | `test_computed_not_null_is_inferred` | Constants, `count(...)`, the ranking window functions, `merge_action()` and a `COALESCE` with a proven argument are NOT NULL (`ComputedExpr`). |
| correct | `test_computed_stays_nullable_when_unproven` | `NULL::t`, `count(*) FILTER`, other aggregates, arithmetic over a proven value and a constant on the null-extended side of an outer join stay nullable. |
| correct | `test_shadowed_function_stays_nullable` | A user-defined `count` or `row_number` outside `pg_catalog` disables the function items for that describe call. Constants and `COALESCE` over them are unaffected. |
| correct | `test_bang_narrows_computed_column` | `"col!"` forces NOT NULL on a computed column (`OverrideBang`). |

## Checker limits

- **Array nesting depth is not checked.** Postgres does not record dimensions in a column's declared type, so
  `describe` can't report them. The checker accepts `list[X]`, `list[list[X]]` and deeper for any array column. A
  mismatch with the real value's dimensions (such as `list[int]` for a two-dimensional `int[]`) surfaces at runtime.
  Pinned by `test_array_depth_is_not_checked`.

## Execution limits

- **No multi-statement execution.** A query is one statement. Postgres prepares one statement at a time, so a string
  with several statements fails at prepare time. Run them as separate queries.
- **No `command` tag in `QueryResult`.** `QueryResult` carries `rows_affected` only. The tag (`"INSERT 0 5"`) is not
  exposed by the driver.
- **Extension types are verified once per statement.** The owning extension of `citext`, `hstore`, `ltree`, `lquery` and
  `cube` is checked when a statement is prepared. `DROP EXTENSION` or `ALTER EXTENSION` while a connection holds a
  cached statement is not noticed until that statement is prepared again.

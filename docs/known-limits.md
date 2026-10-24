# Known limits

Nullability inference limits from [design §8.5](design.md), each pinned by a test in
`src/rowx/__tests__/integration/describe/test_known_limits.py`. A `limit` is a case where rowx is less precise than the
database allows. A `correct` case is a hard-looking shape that rowx gets right.

| Kind | Test | Behaviour |
|---|---|---|
| correct | `test_lateral_on_outer_join` | A LATERAL subquery on the null-extended side is nullable (`OuterJoin`). |
| correct | `test_window_over_outer_join` | A window function over an outer join, behind a subquery, is nullable (`ScanBoundary`). |
| correct | `test_nested_cte_chain` | A nested CTE chain over an outer join is nullable (`ScanBoundary`). |
| limit | `test_scan_boundary_over_promotes` | Every column of a scan boundary alias is nullable, even those from the non-null side. |
| limit | `test_view_columns_are_nullable` | View columns are always nullable (`ForeignOrView`). Use `"col!"` to narrow. |
| limit | `test_set_operation_is_nullable` | Set operation columns have no table origin, so they default to nullable. |
| limit | `test_recursive_cte_is_nullable` | Recursive CTE columns have no table origin, so they default to nullable. |
| limit | `test_computed_column_is_nullable` | Computed columns are always nullable; narrowing needs a parser (design §11). |
| correct | `test_bang_narrows_computed_column` | `"col!"` forces NOT NULL on a computed column (`OverrideBang`). |

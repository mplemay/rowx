# Spike #107: nullability for set operations, recursive CTEs and computed columns

Part of the validation plan [#93](https://github.com/mplemay/rowx/issues/93). This is a write-up only. No production
code changes, and [the design](../design/nullability.md) is untouched. Accepted items need their own issue and a design
change first.

## Method

- **Baseline.** The precision report from #109 (`nullgen.py`), over 8 seeds (`107` and `1` to `7`) of 3,000 statements
  each. That is 24,000 statements, with 60,697 columns that describe marks nullable. Of those, 18,882 never held a NULL
  in the seed data. That count is an upper bound on false positives, because a column can be nullable by schema and
  still hold no NULL in a small seed.
- **Prototype.** A throwaway Python walk over `EXPLAIN (GENERIC_PLAN, VERBOSE, COSTS OFF, FORMAT JSON)`, applied only
  to columns that describe marks nullable with reason `Computed`. It reuses the join marking of `explain.rs`, including
  `Anti` and `Right Anti`. It was not merged and no production code changed.
- **Oracle.** Every column the prototype calls NOT NULL is checked against the rows the statement returns. A NULL in
  such a column is a soundness failure. The handwritten adversarial cases (swapped alias lists, duplicate columns,
  NULL constants, nested set operations, grouping sets under a union, outer-join branches) also pass, with the one
  exception noted below.
- **Soundness result for all three limits:** 0 failures in 24,000 fuzzed statements, run twice (with and without an
  arithmetic whitelist for recursive CTEs).

## Where the false positives are

| Bucket (`Computed` reason) | Nullable | Never NULL (upper bound on false positives) | Removed by the prototype |
|---|---|---|---|
| Set operations | 6,441 | 3,216 | 2,778 |
| Computed columns (`count`, windows, `COALESCE`, constants) | 1,000 | 635 | 444 |
| DML `RETURNING` computed (`merge_action()`) | 264 | 264 | 264 |
| Recursive CTEs | 647 | 534 | 0 (229 with a strict-arithmetic whitelist) |

Against all 18,882 never-NULL nullable columns across every reason, the prototype removes 3,486 (18.5%), or 3,715
(19.7%) with the arithmetic whitelist. The other reasons (`GroupingSets`, `ScanBoundary`, `OuterJoin` and so on) are
outside this spike.

## Set operations

**Recommendation: accept.** Needs a design change, then its own issue.

- Rule, by output position (not by name):
  - `Append`/`Merge Append` (UNION, UNION ALL): NOT NULL only if every branch is NOT NULL at that position.
  - `SetOp` EXCEPT: NOT NULL if the left branch is. INTERSECT: NOT NULL if either branch is, because an intersected
    value must equal a value in that branch.
  - Pass-through nodes (`Aggregate`, `Unique`, `Sort`, `Limit`) are followed only when the child's `Output` equals their
    own.
- Position works where names don't. A set operation's `Output` is named after its first branch, and duplicate or
  swapped names make a name map unsound, so the prototype never maps by name here.
- `Append` and `Recursive Union` nodes have no `Output` key. Use the first child's.
- A leaf is NOT NULL only for a plain column of a `relkind r/p` base table that passes the existing NOT NULL tests and
  isn't null-extended by an ancestor join or a grouping-sets aggregate. The planner has already inlined views and
  subqueries, so branches are positional at that point.
- Result: 2,778 of 3,216 removed (86%). About 280 of the removals come from the computed whitelist applied inside
  branches (for example `count(*) AS n`). Ablation on seed 107: set operation positions alone add about 154 on top of
  the whitelist.
- Not covered and left nullable: a NULL constant branch, a nested set operation under a pass-through node the prototype
  does not follow, and columns behind a `Subquery Scan` or `CTE Scan` with pruned output.

## Recursive CTEs

**Recommendation: do not pursue now.**

- A `CTE Scan` has no column alias list in the plan, and its `Output` is pruned. `WITH RECURSIVE r(b, a) AS (SELECT a, b
  ...)` makes `r.a` mean the second column, and name matching can't tell. Without a parser there is no sound mapping
  from `r.col` to a position.
- The one sound rule without positions is all or nothing: a boundary column is NOT NULL only if every column of the
  recursive union is. That removed 0 of 534 on the fuzzer, because a counter column such as `lvl_ + 1` is computed.
- Whitelisting strict arithmetic (`(col + const)`) gets 229 of 534 (43%), with 0 soundness failures. But it trusts the
  printed operator: a user-defined `+` on the same types could return NULL, and the plan prints it unqualified. A
  guard needs `pg_operator`.
- The work-table reference is treated as NOT NULL by induction, and falls back to untrusted unless every position
  passes. That is sound and was needed (a recursive term can reintroduce NULL).
- Revisit with roadmap item 7 (a parser), which supplies the alias list.

## Computed columns

**Recommendation: accept a small whitelist, with a search-path guard.** Needs a design change, then its own issue.

- Whitelist, parsed with a balanced-parenthesis scan (not a regex over the whole expression, because `count(a) + f(y)`
  would match a regex ending in `)`):
  - non-NULL number, string and boolean constants (`NULL::integer` is not one);
  - `count(...)`, with no `FILTER`, plain or with `OVER`;
  - `row_number()`, `rank()`, `dense_rank()`, `percent_rank()`, `cume_dist()` with `OVER`;
  - `merge_action()`;
  - `COALESCE(...)` when any argument is NOT NULL under these rules.
- Result: 444 of 635 computed columns and 264 of 264 `merge_action()` columns removed. `lag`, `lead`, `first_value`,
  `sum` and `max` stay nullable (correctly), and so does `count(...) FILTER (...)` (imprecise only).
- **Soundness hazard.** The plan prints a function by bare name when it is visible on the `search_path`. With
  `search_path = myschema, pg_catalog`, a user-defined `count(int)` or `row_number()` prints as `count(a)` and
  `row_number()`, identical to the built-ins, and returned NULL in the spike. The fuzzer can't find this because it
  never defines such functions. A plan-only whitelist is therefore unsound unless guarded. Proposed guard: skip the
  whitelist when `pg_proc` has a function with a whitelisted name outside `pg_catalog` in any schema on the session
  `search_path`. This is a design question for the follow-up.
- Strict `pg_proc` checks for general functions are out of scope: they need overload resolution, which the plan doesn't
  give.

## Bugs found

| Bug | Where | Status |
|---|---|---|
| The design says "semi and anti joins never null-extend", which is wrong. `Anti` and `Right Anti` do, and a prototype that followed the doc produced a NOT NULL verdict that a NULL contradicted. | `docs/design/nullability.md` (the code in `explain.rs` is right) | Filed as [#131](https://github.com/mplemay/rowx/issues/131) |

One handwritten expectation was wrong, not the prototype: `count(*) FILTER (...)` is NOT NULL, and the prototype
conservatively keeps it nullable.

## Not tested

- Plan shapes that need many rows, parallel plans (`Gather`) and partitioned tables. The seed data is small.
- Parameterised statements and the plan cache. Spike statements have no parameters, as in the #109 fuzzer.
- The prototype ran through the fuzzer's generators, which have no nested set operations or user-defined functions.
  The follow-up should extend the fuzzer first.

# Describe and nullability (`rowx-pg/src/describe.rs`, `explain.rs`)

## Interface and checker session

```rust
pub struct Describe { pub params: Vec<ParamInfo>, pub columns: Vec<ColumnInfo> }
pub struct ParamInfo  { pub oid: u32, pub type_name: String, pub type_key: Result<&'static str, Unsupported> }
pub struct ColumnInfo {
    pub name: String,                     // override suffix stripped
    pub raw_name: String,                 // as returned by the server
    pub oid: u32, pub type_name: String, pub type_key: Result<&'static str, Unsupported>,
    pub table_oid: Option<u32>, pub attnum: Option<i16>,
    pub nullable: bool,                   // final, after defaulting
    pub reason: NullReason,
}
pub enum NullReason { NotNullColumn, NullableColumn, NotValidatedConstraint, ForeignOrView,
    OuterJoin { node: String }, GroupingSets, ScanBoundary { alias: String },
    ReturningOldNew, Computed, ExplainFailed, OverrideBang, OverrideQuestion }
```

The Python binding is `rowx._core.describe(conn, sql) -> Describe`. When the server
rejects the SQL it raises `DatabaseError` with `.position` set.

**Checker session.** On connect, the checker runs:

- `SET default_transaction_read_only = on`;
- `SET statement_timeout = <cfg, default 10s>`;
- `SET lock_timeout = '2s'`;
- `SET idle_in_transaction_session_timeout = '30s'`;
- `SET application_name = 'rowx-check'`.

**Safety statement** (this exact wording goes in the docs):

- The checker performs Parse/Describe, catalog lookups and `EXPLAIN` only, never
  `ANALYZE`. It does not execute your statements.
- Postgres may still **evaluate user-defined IMMUTABLE and STABLE functions while
  planning**, and it takes locks on referenced relations.
- Run it as a dedicated non-superuser role against a CI or disposable database.

## Algorithm

1. **Prepare** with `client.prepare(sql)`.
   - A server error is `sql-error`, carrying its position.
   - Multi-statement strings fail here, and this is a security invariant: step 3
     concatenates the SQL into a simple-protocol query, so it relies on Parse having
     rejected anything with a second command. It is covered by a test.
   - `ParamInfo` and `ColumnInfo` come from `Statement::params()`/`columns()`, using
     `Column::table_oid()`/`column_id()`. Unsupported types are resolved through
     `pg_type.typsend`.
2. **Base nullability.** One batched catalog query covers every column that has a table
   origin. It joins `pg_attribute a`, `pg_class c ON c.oid = a.attrelid` and
   `pg_constraint`.
   - **NotNull** (`NotNullColumn`) requires all of:
     - `a.attnotnull`;
     - `c.relkind IN ('r','p','m')`;
     - `NOT EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conrelid = a.attrelid AND k.contype = 'n'
       AND NOT k.convalidated AND k.conkey = ARRAY[a.attnum])`.
       PG18 `NOT VALID` NOT NULL constraints set `attnotnull` even though NULL rows still exist.
   - A foreign table or view gets `ForeignOrView` (Nullable). A non-validated constraint
     gets `NotValidatedConstraint` (Nullable). Any other column with a table origin gets
     `NullableColumn`.
   - A column with no table origin gets `Computed` (Unknown).
3. **Plan pass.** This step is skipped when the statement returns 0 columns.
   - Run `EXPLAIN (GENERIC_PLAN, VERBOSE, COSTS OFF, FORMAT JSON)` + sql through
     **`simple_query`**. Over the extended protocol, EXPLAIN would demand values for `$n`.
   - If EXPLAIN fails, keep the base results and mark Unknown columns `ExplainFailed`.
     This is non-fatal. Utility statements that return columns, such as `SHOW`, `CALL` and
     `FETCH`, fail EXPLAIN with 42601.
   - The walk is described in [Plan walk](#plan-walk-explainrs-a-pure-function-over-serde_jsonvalue). Its result can
     only promote a column to Nullable, never the reverse.
4. **Overrides** ([Overrides](#overrides)).
5. **Defaulting.** An Unknown column becomes Nullable (SQLx). Computed columns therefore
   need `"col!"`.

### Plan walk (`explain.rs`, a pure function over `serde_json::Value`)

Input is the plan JSON and the column count. Output is `Vec<Option<NullReason>>`, the
promotions for each top-level column position.

- **Top outputs.** Take the top node's `Output` list. Every top position whose expression
  string equals a marked expression is promoted. Duplicate columns are each promoted.
- **Join marking.** At a node with `Join Type`, mark the nullable children:
  - `Full`: both children;
  - `Left`: the child whose `Parent Relationship` is `Inner`;
  - `Right`: the child whose `Parent Relationship` is `Outer`.

  Semi and anti joins never null-extend. A marked subtree stays marked, and its `Output`
  expressions become marked.
- **Descend through every node type.** That includes Sort, Limit, Aggregate, Hash,
  Materialize, Gather, Append and SubPlans/InitPlans, not only joins at the top.
- **Scan boundaries.** These cover a `Subquery Scan`, `CTE Scan` or `WorkTable Scan` with
  alias `x`. For a CTE, the source subtree is the InitPlan whose `Subplan Name` is
  `CTE x`.
  - If that source subtree contains any null-extending join, or a grouping-sets aggregate,
    every top output that starts with `x.` is promoted (`ScanBoundary`).
  - This is deliberately conservative. Mapping positions precisely is future work.
- **Grouping sets.** An `Aggregate` node with a `"Grouping Sets"` key holding more than
  one set marks its own `Output` (`GroupingSets`).
- **RETURNING old/new (PG18).** On a `ModifyTable` top node:
  - outputs starting with `old.` are promoted for `Operation` Insert or Merge;
  - outputs starting with `new.` are promoted for Delete or Merge.

  If a `RETURNING WITH (OLD AS o, NEW AS n)` alias makes the prefix ambiguous, promote
  every output for Merge, and promote by the declared aliases otherwise.

## Overrides

If the raw column name ends in `!`, strip the suffix and set NotNull (`OverrideBang`). If it
ends in `?`, strip it and set Nullable (`OverrideQuestion`). Overrides are applied last and
always honoured.

## Evidence: why not sqlx's describe

Measured on Postgres 18 in the spike and the review. The schema is
`users(id pk, name not null, email null)` and `posts(id pk, user_id not null, title not null)`.

| Query | Column | Truth | sqlx 0.9.0 | rowx ([Algorithm](#algorithm)) |
|---|---|---|---|---|
| `users u LEFT JOIN posts p … WHERE u.id = $1` | p.title | nullable | **NOT NULL** ✗ | nullable |
| `posts p RIGHT JOIN users u` | p.title | nullable | **NOT NULL** ✗ | nullable |
| `LEFT JOIN … ORDER BY p.title` | p.title | nullable | **NOT NULL** ✗ | nullable |
| trivial `(SELECT … LEFT JOIN …) x` (the planner elides it) | x.title | nullable | **NOT NULL** ✗ | nullable |
| `(SELECT p.title, row_number() over () … LEFT JOIN …) x` | x.title | nullable | ✗ | nullable (ScanBoundary) |
| `WITH c AS MATERIALIZED (… LEFT JOIN …) SELECT title FROM c` | title | nullable | ✗ | nullable (ScanBoundary) |
| `SELECT name FROM users GROUP BY ROLLUP(name)` | name | nullable | ✗ | nullable (GroupingSets) |
| `INSERT … RETURNING old.name` | name | nullable | ✗ | nullable (ReturningOldNew) |
| `SELECT p.title, p.title … LEFT JOIN` | both | nullable | ✗ | both nullable |
| column with `NOT NULL … NOT VALID` | x | nullable | ✗ | nullable |
| `FULL JOIN` | both | nullable | nullable | nullable |
| `LEFT JOIN … WHERE p.title IS NOT NULL` (the planner reduces it to an inner join) | p.title | not null | – | not null ✓ |
| `upper(name)`, `count(*)`, `coalesce(..)`, `1` | – | – | unknown | unknown → nullable |

sqlx gets these wrong for three reasons:

1. It EXPLAINs `EXECUTE stmt(NULL…)` without forcing a generic plan, so `= NULL`
   constant-folds the join away.
2. It treats `Inner` as the nullable side of a right join.
3. It only walks joins at the top level.

Its `describe` is also `#[doc(hidden)]` and feature-gated. rowx keeps sqlx's **model**.

## Known limits (pinned by tests, documented)

- Matching expression strings is heuristic. LATERAL, window functions over outer joins,
  and nested CTE chains are covered by the conservative scan-boundary rule. They are
  pinned by `tests/describe/test_known_limits.py`.
- **Views** are always Nullable, because `attnotnull` is false for view columns.
- **Set operations and recursive CTEs** have `table_oid = 0`, so they are Unknown →
  Nullable. That is safe.
- **Computed columns** are always Unknown → Nullable. NOT NULL narrowing for computed
  columns would need a parser ([Dependency evaluation](dependencies.md#dependency-evaluation)).

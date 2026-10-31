# Describe and nullability (`rowx-postgres/src/describe.rs`, `explain.rs`)

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
pub enum NullReason { NotNullColumn, NullableColumn, NotValidatedConstraint, ForeignOrView, ViewColumn,
    OuterJoin { node: String }, GroupingSets, ScanBoundary { alias: String },
    ReturningOldNew, Computed, ExplainFailed, OverrideBang, OverrideQuestion }
```

The Python binding is `rowx._core.postgres.describe(conn, sql) -> Describe`. When the server
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
   - A foreign table gets `ForeignOrView` (Nullable). A view column (`relkind = 'v'`) is resolved by
     [View columns](#view-columns); when that does not prove NOT NULL it stays Nullable, with `ForeignOrView` for a
     fallback and `ViewColumn` when the definition was analysed. A non-validated constraint
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
     only promote a column to Nullable, never the reverse. The exceptions are a Computed column behind a set
     operation, which [Set operations](#set-operations) can prove NOT NULL (`SetOperation`), and a Computed column
     whose expression [Computed columns](#computed-columns) proves NOT NULL (`ComputedExpr`).
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
  - `Right`: the child whose `Parent Relationship` is `Outer`;
  - `Anti`: the child whose `Parent Relationship` is `Inner`;
  - `Right Anti`: the child whose `Parent Relationship` is `Outer`.

  Semi joins never null-extend. An anti join null-extends the other side's columns: they can
  still appear in the plan's `Output`, and are NULL on every output row. A marked subtree stays marked, and its `Output`
  expressions become marked.
- **Descend through every node type.** That includes Sort, Limit, Aggregate, Hash,
  Materialize, Gather, Append and SubPlans/InitPlans, not only joins at the top.
- **Scan boundaries.** These cover a `Subquery Scan`, `CTE Scan` or `WorkTable Scan` with
  alias `x`. For a CTE, the source subtree is the InitPlan whose `Subplan Name` is
  `CTE x`.
  - If that source subtree contains any null-extending join, or a grouping-sets aggregate,
    every top output that starts with `x.` is promoted (`ScanBoundary`).
  - This is the **conservative rule**. It is refined by the [mapping rule](#scan-boundary-mapping) below, which can
    only remove promotions the conservative rule would add.
- **Grouping sets.** An `Aggregate` node with a `"Grouping Sets"` key holding more than
  one set marks its own `Output` (`GroupingSets`).
- **RETURNING old/new (PG18).** On a `ModifyTable` top node:
  - outputs starting with `old.` are promoted for `Operation` Insert or Merge;
  - outputs starting with `new.` are promoted for Delete or Merge.

  If a `RETURNING WITH (OLD AS o, NEW AS n)` alias makes the prefix ambiguous, promote
  every output for Merge, and promote by the declared aliases otherwise.

### Scan-boundary mapping

A `Subquery Scan` or `CTE Scan` has no column-alias list in the plan JSON, and its `Output` is pruned to the columns
the parent uses (in `cte_materialized.json`, `CTE Scan c` outputs only `c.title`, while the source `Hash Join` outputs
`u.name` and `p.title`). A boundary output can therefore be traced to its source **only by name**.

**Rule.** For a boundary alias `x` whose source subtree contains a null-extending join or grouping-sets aggregate, a top
output `x.col` is promoted (`ScanBoundary`) unless all of these hold, in which case it is not promoted:

1. the source node's `Output` has exactly one entry whose column name is `col` (the part after the last `.` of a
   qualified entry, or the whole entry when unqualified);
2. that entry is a plain column reference `rel.col`, not an expression, function call, cast or constant;
3. the entry, by the ordinary walk of the source subtree, is not marked nullable (`OuterJoin`, `GroupingSets`);
4. the entry's identity is proven: the entry `rel.col` resolves to the same base column the describe origin reports for
   the top column (`table_oid`, `attnum`), that is, `rel` is a scan alias of that table in the source subtree and `col`
   is that attribute's name.

Step 4 is needed because the plan carries no alias mapping, so name equality alone does not establish identity. A
subquery or CTE can rename columns: in `WITH c AS MATERIALIZED (SELECT p.title AS name, u.name AS title FROM users u
LEFT JOIN posts p ON …) SELECT name FROM c`, `c.name` is `p.title`, but the source outputs `p.title` and `u.name`
match `c.name` by name only to `u.name`. The origin of `c.name` is `posts.title`, which differs from `users.name`, so
step 4 fails and the column is promoted. A top column with no origin (`table_oid = 0`) always fails step 4.

Names are compared only when both the alias and the column are plain unquoted identifiers. A quoted or dotted name
(`"Title"`, an uppercase column) uses the conservative rule, so such columns are not narrowed.

A source subtree that merges inputs (`Append`, `Merge Append`, `SetOp`, `Recursive Union`) also uses the conservative
rule, because such a node names its output after its first input. Describe reports no origin for a set-operation
column, so step 4 already fails there. The check is defensive and covers a change in what the server reports.

Any other case uses the conservative rule: promote. The mapping never promotes an output the conservative rule would
not, and the walk stays promote-only.

| Case | Result |
|---|---|
| Source output is a computed expression (`upper(p.title)`, `p.title \|\| ''`) | Promote. Rule step 2 fails. |
| Two source outputs share a name (`u.id`, `p.id`) | Promote every `x.id`. Rule step 1 fails. |
| A `MATERIALIZED` CTE referenced twice (`CTE Scan a`, `CTE Scan b`) | Each alias is mapped independently against the one `CTE c` InitPlan. The source nullability is the same, so each result is also the same. |
| The planner replaced an unused column with a NULL constant | Promote. It is a constant, so step 2 fails. |
| The source node has no `Output` entry for `col` (pruned differently) | Promote. Step 1 fails. |
| Columns renamed by the subquery or CTE (swapped aliases) | Promote. The name match points at a different column than the origin. Step 4 fails. |
| Nested boundaries (a CTE over a subquery) | Map one boundary at a time. Resolve the inner boundary first, and treat an entry it promotes as marked in step 3. |

### Set operations

A set-operation column has no table origin, so it starts as Computed (Unknown). The plan can prove it NOT NULL, by
output position and never by name. A Computed column that is proven becomes NOT NULL with reason `SetOperation`. The
proof only applies to Computed columns, and a promotion from the walk above still wins.

For a top-level output position `i`, the node is NOT NULL at `i` when:

- `Append` or `Merge Append` (`UNION`, `UNION ALL`): it has at least one child and every child is NOT NULL at `i`.
  These nodes have no `Output` key, so they take their first child's when compared with a parent.
- `SetOp`: `EXCEPT` is NOT NULL if its left (`Outer`) child is; `INTERSECT` is NOT NULL if either child is.
- `Aggregate`, `Unique`, `Sort` and `Limit`: it has exactly one child, its `Output` is non-empty and equals the child's
  `Output`, and the child is NOT NULL at `i`. Anything else, including a changed `Output`, is nullable.
- A scan node (`Seq Scan`, `Sample Scan`, `Index Scan`, `Index Only Scan`, `Bitmap Heap Scan`, `Tid Scan`): the `i`th
  output is `alias.col` where the alias is the node's `Alias`, both names are plain unquoted lowercase identifiers, the
  relation is `relkind` `r` or `p`, the column passes the NOT NULL tests of the [algorithm](#algorithm) (attnotnull and
  no `NOT VALID` constraint), and the expression is not marked as null-extended by an ancestor join or a
  grouping-sets aggregate.
- Any other node (a join, a `Subquery Scan`, a `Result`, a NULL or computed output, a recursive union) is nullable.

The catalog lookup for the scanned columns is one batched query, and only runs when the plan contains an `Append`,
`Merge Append` or `SetOp`. If it fails, nothing is proven and the columns stay nullable.

### Computed columns

A Computed column can be proven NOT NULL from the printed `Output` expression of the plan's root node at that output
position. A proven column becomes NOT NULL with reason `ComputedExpr`. No SQL parser is needed: the expression is
read with a balanced-parenthesis scan that skips quoted strings and identifiers, never a regex over the whole
expression. Outer parentheses are stripped only while the first one closes at the end of the text. The expression is
NOT NULL when it is exactly one of:

- a non-NULL numeric, string (optionally cast to a plain type name) or boolean constant. `NULL::t` is not one;
- `count(...)` without a `FILTER` clause, plain or followed by `OVER`;
- `row_number()`, `rank()`, `dense_rank()`, `percent_rank()` or `cume_dist()` with no arguments and an `OVER`
  clause (the hypothetical-set aggregates `rank(x) WITHIN GROUP` are not these functions);
- `MERGE_ACTION()`. The plan deparses the built-in as special syntax in capitals, so it needs no guard: `merge_action()`
  always parses as that syntax and a user-defined function cannot take its place;
- `COALESCE(a, b, ...)` when any top-level argument is NOT NULL under these rules.

Anything after the call, such as an operator or a cast, makes the expression nullable. Two further rules keep the
proof sound:

- **Null-extension applies at every level.** An expression that an outer join null-extends (the walk's marked set) is
  nullable, and so is a `COALESCE` argument that is, even when the argument is itself a whitelisted call such as
  `(count(*))`.
- **Set operations decide for themselves.** Above an `Append`, `Merge Append` or `SetOp`, pass-through parents
  (`Aggregate`, `Unique`, `Sort`, `Limit`, `Gather` and similar) print the first branch's expression for every branch,
  so `SELECT 1 UNION SELECT NULL::int` prints `(1)`. When the root's outputs are carried up from a set operation, the
  computed proof does not apply and only [Set operations](#set-operations) can prove the column. An aggregate over a
  set operation prints its own expression, so `count(*)` over a `UNION` is still proven.

A set-operation proof is tried first, and neither overrides a promotion from the walk.

**Shadowing guard.** The plan prints a function by bare name, so with `search_path = myschema, pg_catalog` a
user-defined `count` or `row_number` is indistinguishable from the built-in and can return NULL. The guard is one
catalog query, `pg_proc` joined to `pg_namespace` for a function with a whitelisted name in a schema other than
`pg_catalog`. If it returns a row, or fails, the function-name items are disabled. Constants and `COALESCE` over them
are unaffected. This avoids parsing `search_path`. The query runs inside `describe`, after the plan pass, and only
when a function name is what proves a column, so a query that needs no function proof costs no round trip. The result
is not cached across calls: `describe` holds no per-connection state, the query is cheap, and a cache would go stale
after `CREATE FUNCTION` or `DROP FUNCTION`. A view definition that is analysed inside the same call runs its own plan
pass and so its own guard.

### View columns

A view column has `attnotnull = false`, so base nullability alone is always Nullable. rowx analyses the view's
definition and gives the column NOT NULL only when the definition proves it.

1. **Eligibility.** The column's table has `relkind = 'v'`. The following are always Nullable (`ForeignOrView`),
   without analysis:
   - the view has any `pg_rewrite` rule other than its own `_RETURN` rule (`rulename <> '_RETURN'`), such as an
     `INSTEAD` rule;
   - `security_invoker` is set and the checker's role cannot `SELECT` every underlying relation (otherwise the
     definition cannot be planned the way the query would);
   - the resolution depth exceeds 8, or the view was already visited in this chain (a cycle, which Postgres rejects at
     `CREATE VIEW` but the check is cheap and keeps the walk total).

   `security_barrier` and `WITH CHECK OPTION` do not change which rows are returned, and need no fallback.
2. **Definition resolution.** Read `pg_get_viewdef(oid, false)`. Its text is schema-qualified only as needed for the
   *current* `search_path`, so describing it under the checker's `search_path` could resolve a name to a different
   table. rowx therefore runs `SET LOCAL search_path = pg_catalog` in one transaction that is rolled back afterwards,
   and calls `pg_get_viewdef` **and** describes the text inside it. Only `pg_catalog` is searched, so `pg_get_viewdef`
   qualifies every other name and an unqualified one cannot resolve. A definition that fails to describe falls back to
   Nullable. Building the query from `pg_rewrite` (`ev_action`) is rejected: that is an internal node tree with no
   stable, supported text form.
3. **Recursion.** The definition is described like any query: base nullability (which resolves view columns inside it
   with the same algorithm) and the plan pass. Its per-column verdict is cached per view OID for the checker run and
   dropped with the statement cache on a schema epoch change.
4. **Mapping.** The view's column with `attnum = n` (live, non-dropped columns, counted in order) maps to output
   position `n'` of its definition, where `n'` is the rank of `attnum` among the view's non-dropped columns. The
   verdict of that position becomes the verdict of the column, with reason `ViewColumn`. Only NOT NULL results change
   anything: a Nullable or Unknown definition column stays Nullable.
5. **Invariant.** The definition ends with `;`. It is concatenated into the simple-protocol `EXPLAIN` only after it has
   passed Parse as a single statement, and with the trailing `;` stripped. A definition that fails Parse, or contains
   a second statement, is Nullable (`ForeignOrView`). A test feeds a crafted definition.

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
| `upper(name)` | – | – | unknown | unknown → nullable |
| `count(*)`, `coalesce(name, 'x')`, `1` | not null | – | unknown | not null ([Computed columns](#computed-columns)) |

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
- **Views.** A view column is NOT NULL only when its definition proves it ([View columns](#view-columns)). Views with
  extra rules, unreadable `security_invoker` views, definitions that fail to describe, cycles and chains deeper than 8
  stay Nullable.
- **Scan boundaries.** A boundary column is not promoted only on an exact, unique name match to a plain,
  non-null-extended source column that resolves to the origin column
  ([Scan-boundary mapping](#scan-boundary-mapping)). Computed, duplicate-named, constant and renamed (swapped-alias)
  outputs still promote.
- **Set operations** are NOT NULL only when [the plan proves it](#set-operations). Branches that are joins, subquery
  scans, casts, computed or constant expressions stay nullable. **Recursive CTEs** have `table_oid = 0`, so they are
  Unknown → Nullable. That is safe.
- **Computed columns** are NOT NULL only when [the whitelist proves it](#computed-columns). Anything else
  (`upper(name)`, `id + 1`, `count(*) FILTER (...)`, `sum(...)`) is Unknown → Nullable, and a user-defined function
  that shadows a whitelisted name disables the function items.

# Nullability

Describe prepares a statement, reads catalog NOT NULL, and walks the generic plan. Unknown columns count as nullable.
`"col!"` and `"col?"` overrides apply last. The plan pass can promote a column to nullable. A set operation, the
computed-column whitelist, or a scan filter can prove a column NOT NULL. A later promotion still forces the column
nullable.

The plan walk lives in `crates/rowx-postgres/src/explain.rs`. Catalog nullability and the describe session live in
`crates/rowx-postgres/src/describe.rs`. The Python binding is `rowx_postgres._core.describe`. Column-name override
stripping is `column_name` in `crates/rowx-core/src/row_shape.rs`, so describe and named-row matching cannot disagree.

## Interface and checker session

When the server rejects the SQL, describe raises `DatabaseError` with `.position` set.

On connect, the checker session runs:

- `SET default_transaction_read_only = on`;
- `SET statement_timeout` from config, default `10s`;
- `SET lock_timeout = '2s'`;
- `SET idle_in_transaction_session_timeout = '30s'`;
- `SET application_name = 'rowx-check'`.

Safety statement:

- The checker performs Parse/Describe, catalog lookups and `EXPLAIN` only, never `ANALYZE`. It does not execute your
  statements.
- Postgres may still **evaluate user-defined IMMUTABLE and STABLE functions while planning**, and it takes locks on
  referenced relations.
- Run it as a dedicated non-superuser role against a CI or disposable database.

With `--scratch` ([Drift gate and scratch](checker.md#drift-gate-and-scratch)) the statement is amended:

- The checker still never imports or executes user Python, and never executes checked queries.
- It does execute the **migration files**, and only in the database it created for the run, as the connecting role.
  Migration files are trusted code ([Security](security.md)). The role needs `CREATEDB`.

## Algorithm

1. **Prepare** with `client.prepare(sql)`. A server error is `sql-error`. Multi-statement strings fail here. Parse
   accepts exactly one statement, and step 3 explains only that prepared statement by name. Unsupported types are
   resolved through `pg_type.typsend`.
2. **Base nullability.** One batched catalog query covers every column that has a table origin.
   - **NotNull** (`NotNullColumn`) requires `attnotnull`, `relkind IN ('r','p','m')`, and no `NOT VALID` NOT NULL
     constraint on that column. PostgreSQL 18 sets `attnotnull` for a `NOT VALID` constraint even though NULL rows
     can still exist (`NotValidatedConstraint`).
   - A foreign table is `ForeignOrView` (nullable). A view column is resolved by [View columns](#view-columns). A
     column with no table origin is `Computed` (unknown).
3. **Plan pass.** Skipped when the statement returns 0 columns. No SQL text, from the user or from a view, is ever
   sent in an `EXPLAIN`.
   - **Domain parameters.** `EXPLAIN … EXECUTE` evaluates its arguments. A statement with a top-level domain
     parameter is prepared again with each domain typed as its fully unwrapped base type, and only that statement is
     explained. A `NULL` array, range or composite does not need unwrapping. This is a second Parse, never
     concatenation. No domain `CHECK` runs.
   - **Find the server-side name.** One query on `pg_catalog.pg_prepared_statements` matches the statement text and
     parameter types, with every catalog object qualified. The pick is `ORDER BY name LIMIT 1`. If nothing matches,
     the plan pass fails as below.
   - **Explain by name.** One simple-query batch:
     `SET LOCAL plan_cache_mode = force_generic_plan; EXPLAIN (VERBOSE, COSTS OFF, FORMAT JSON) EXECUTE "<name>"(NULL, …)`.
     The name is quoted as an identifier. Zero parameters have no parentheses. `plan_cache_mode` is set in this batch,
     not at connect, so the session's own mode is unchanged afterwards and a custom plan cannot fold `= NULL` and drop
     an outer join.
   - The response must be exactly one result set, and its JSON must have a `Plan` key. A prepared `SHOW` or `CALL`
     fails here.
   - If the plan pass fails, base results are kept and unknown columns are marked `ExplainFailed`. This is
     non-fatal.
   - The plan pass can promote a column to nullable. A computed column that
     [Set operations](#set-operations) prove NOT NULL is `SetOperation`, and one that
     [Computed columns](#computed-columns) prove NOT NULL is `ComputedExpr`. A catalog-nullable simple or enum
     column is `ScanFilterNotNull` when `scan_filter_not_null` matches a childless `Seq Scan`, `Index Scan`, or
     `Index Only Scan`: the output text equals the plain column or `alias.column`, the origin schema and relation
     match the scan, and the filter text is exactly `(alias.column IS NOT NULL)`. The alias and the column are
     plain unquoted lowercase identifiers. A later promotion still forces the column nullable. Overrides still
     apply last.
4. **Overrides** ([Overrides](#overrides)).
5. **Defaulting.** An unknown column becomes nullable. A computed column outside the whitelist therefore needs
   `"col!"`.

## Plan walk

Input is the plan JSON and the column count. Output is a promotion for each top-level column, or none.

- **Top outputs.** Every top position whose expression string equals a marked expression is promoted. Duplicate
  columns are each promoted.
- **Join marking.** At a node with `Join Type`, mark the nullable children:
  - `Full`: both children;
  - `Left`: the child whose `Parent Relationship` is `Inner`;
  - `Right`: the child whose `Parent Relationship` is `Outer`;
  - `Anti`: the child whose `Parent Relationship` is `Inner`;
  - `Right Anti`: the child whose `Parent Relationship` is `Outer`.

  Semi joins never null-extend. **Anti joins and right anti joins do.** An anti join null-extends the other side's
  columns: they can still appear in the plan's `Output`, and they are NULL on every output row. A marked subtree
  stays marked.
- **Descend through every node type**, including Sort, Limit, Aggregate, Hash, Materialize, Gather, Append and
  SubPlans, not only joins at the top.
- **Scan boundaries.** A `Subquery Scan`, `CTE Scan` or `WorkTable Scan` with alias `x`. If the source subtree
  contains any null-extending join or a grouping-sets aggregate, every top output that starts with `x.` is promoted
  (`ScanBoundary`), unless the [mapping rule](#scan-boundary-mapping) removes that promotion.
- **Grouping sets.** An `Aggregate` with more than one grouping set marks its own `Output` (`GroupingSets`).
- **RETURNING old/new (PostgreSQL 18).** On a `ModifyTable` top node, outputs starting with `old.` are promoted for
  Insert or Merge, and outputs starting with `new.` are promoted for Delete or Merge. If a `RETURNING WITH (OLD AS o,
  NEW AS n)` alias makes the prefix ambiguous, promote every output for Merge, and promote by the declared aliases
  otherwise.

### Scan-boundary mapping

A boundary output can be traced to its source only by name, and only when that name is proven. For alias `x` whose
source contains a null-extending join or grouping-sets aggregate, a top output `x.col` is **not** promoted only when
all of these hold:

1. the source `Output` has exactly one entry named `col`;
2. that entry is a plain column reference, not an expression, function call, cast or constant;
3. that entry is not itself marked nullable;
4. `rel.col` resolves to the same base column describe reports (`table_oid`, `attnum`).

Anything else is promoted: computed outputs, duplicate names, planner NULL constants, swapped aliases, quoted or
dotted names, and a source that merges inputs (`Append`, `Merge Append`, `SetOp`, `Recursive Union`). The mapping
never promotes an output the conservative rule would not. A `MATERIALIZED` CTE referenced twice is mapped once per
alias. Nested boundaries are resolved inner first.

### Set operations

A set-operation column starts as computed (unknown). The plan can prove it NOT NULL by output position, never by
name (`SetOperation`). A promotion from the walk still wins.

For output position `i`:

- `Append` or `Merge Append` (`UNION`, `UNION ALL`): NOT NULL only if every child is NOT NULL at `i`. These nodes
  have no `Output` key, so they take the first child's when a parent compares them.
- `SetOp`: `EXCEPT` is NOT NULL if the left child is; `INTERSECT` is NOT NULL if either child is.
- `Aggregate`, `Unique`, `Sort` and `Limit`: NOT NULL when the node has one child, its `Output` equals the child's,
  and the child is NOT NULL at `i`.
- A scan of `relkind` `r` or `p`: NOT NULL when the output is a plain `alias.col`, both names are unquoted lowercase
  identifiers, the column passes the NOT NULL tests above, and it is not null-extended by an ancestor.
- Any other node, including a join, a subquery scan, a NULL or computed output, and a recursive union, is nullable.

The catalog lookup for scanned columns runs only when the plan contains an `Append`, `Merge Append` or `SetOp`. If
it fails, nothing is proven.

### Computed columns

A computed column can be proven NOT NULL from the root `Output` expression (`ComputedExpr`). The expression is read
with a balanced-parenthesis scan. It is NOT NULL when it is exactly one of:

- a non-NULL numeric, string or boolean constant (`NULL::t` is not one);
- `count(...)` without `FILTER`, plain or with `OVER`;
- `row_number()`, `rank()`, `dense_rank()`, `percent_rank()` or `cume_dist()` with `OVER`;
- `MERGE_ACTION()`;
- `COALESCE(...)` when any top-level argument is NOT NULL under these rules.

Anything after the call, such as an operator or a cast, stays nullable. An expression an outer join null-extends is
nullable even when the text is a whitelisted call. Above a set operation, pass-through parents print the first
branch's expression, so the computed proof does not apply there and only [Set operations](#set-operations) can prove
the column.

**Shadowing guard.** If `pg_proc` has a whitelisted function name outside `pg_catalog`, or the lookup fails, the
function-name items are disabled for that describe call. Constants and `COALESCE` over them are unaffected. The
lookup runs only when a function name is what would prove a column, and it is not cached across calls.

### View columns

A view column is NOT NULL (`ViewColumn`) only when its definition proves it. The following stay nullable
(`ForeignOrView`) without a proof: any rewrite rule other than `_RETURN`, an unreadable `security_invoker` view, a
cycle, depth over 8, or a definition that fails to describe. `security_barrier` and `WITH CHECK OPTION` need no
fallback.

The definition is read with `pg_get_viewdef` under `SET LOCAL search_path = pg_catalog`, in a transaction that is
rolled back, and described in that same transaction. It is explained by name, never concatenated into an `EXPLAIN`.
The view's `attnum`, ranked among non-dropped columns, selects the definition's output position. Only a NOT NULL
verdict is kept. The per-view verdict is cached for the checker run.

## Overrides

If the raw column name ends in `!`, strip the suffix and force NOT NULL (`OverrideBang`). If it ends in `?`, strip
it and force nullable (`OverrideQuestion`). Overrides are applied last and always honoured. `?column?` is not an
override. Postgres truncates identifiers at 63 bytes; a quoted alias in the source that would lose its suffix is
`invalid-override` ([Nullability overrides](api.md#nullability-overrides)).

## Evidence

Measured on Postgres 18. The schema is `users(id pk, name not null, email null)` and
`posts(id pk, user_id not null, title not null)`. sqlx 0.9.0 is wrong where the table says so, for three reasons: it
explains `EXECUTE` without forcing a generic plan, it treats `Inner` as the nullable side of a right join, and it
only walks joins at the top. rowx keeps sqlx's model and not its describe implementation.

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

## Known limits

The cases pinned by tests, and which of them are imprecise rather than wrong, are in
[Known limits](../known-limits.md). In short: recursive CTE columns stay nullable; a computed column outside the
whitelist stays nullable; a view, set operation or scan boundary is NOT NULL only when the rules above prove it.
Anti joins and right anti joins are not a limit. They null-extend, and the walker marks them.

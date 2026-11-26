# Known limits

Nullability inference limits from [the reference](reference/nullability.md#known-limits), each
pinned by a test in `packages/rowx-postgres/src/rowx_postgres/__tests__/integration/describe/test_known_limits.py`.
A `limit` is a case where rowx is
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

## Named row limits

- **No nesting.** A named type is allowed only as the row. A named type inside a tuple, a list or a union, or as a
  field type, is `invalid-row-type`, so there are no JSON-shaped fields, composite or record columns, or `flatten`.
- **Pydantic-only types are rejected.** `EmailStr`, `AnyUrl` and `SecretStr` aren't row types on either side, and
  `constr()` and `PositiveInt` (constraint aliases of the form `Annotated[<leaf>, ...]`) are rejected by `rowx check`
  only; the runtime strips the metadata and accepts them. The checker compares exact Python types and has no pydantic
  type table. Annotate the column's Python
  type and validate with `Annotated[str, AfterValidator(...)]`. Lax-mode coercions, such as a `numeric` column into an
  `int` field, are reported too.
- **Models with unknown names are partly checked.** For `AliasChoices`, `AliasPath`, a non-literal alias, a
  `model_validator(mode="before" | "wrap")`, an `alias_generator` or an unreadable `model_config`, the checker skips
  `missing-column`, `unexpected-column`, `duplicate-column` and `ambiguous-column` for the whole model. A
  `names not checked` note is attached only to a diagnostic the model emits, and a clean model with unknown names is
  silent. Type and nullability checks still run on columns that map to a field with known names. At
  runtime a column that matches no known name passes through with the default decoder, so an `interval` column there
  has no `Interval` flag, and two columns with one name collapse to the last one in the dict.
- **A generic pydantic dataclass is not parametrized.** `Box[int]` is built by calling the origin class, so pydantic
  validates the field as the unparametrized `T` and values pass through. A `TypeVar` bound is enforced by pydantic there
  but not by the checker.
- **Checker-only rejections beyond pydantic aliases.** `Json[...]`, mixed row type forms (a model with a stdlib
  dataclass base, or a stdlib and a pydantic dataclass stacked), an undecorated subclass of a pydantic dataclass, a
  base from the stdlib (`abc.ABC`, `Exception`), a field declared in a conditional block, and a plain alias over a
  `TypeVarTuple` work at runtime and fail `rowx check`.
- **Notes print absolute paths.** The `field defined at` and similar notes show an absolute path (#208).
- **No column rename for dataclasses and TypedDicts.** There is no equivalent of sqlx's `rename`, so a column that
  needs a different name than the field uses a quoted alias in SQL.
- **Some rejections are checker-only.** A class decorator other than the dataclass decorators and `typing.final`, a
  `metaclass=` or another unknown class keyword, a hand-written `__init__` on a stdlib dataclass, and a pydantic
  constraint alias are rejected by
  `rowx check`, because it can't see what they do. The runtime accepts them, and a constructor failure surfaces as
  `RowConstructError`.
- **Not supported:** attrs, msgspec, `Protocol`, `collections.namedtuple`, `RootModel`, pydantic v1, a dataclass with
  `init=False` or an `InitVar` (a pydantic dataclass accepts the decorator's `init=False` but rejects `InitVar`), a
  named row with no fields, a nullable named row (`User | None`, use `.optional()`), named parameters, and row classes
  from installed packages (the checker resolves classes in the project only). Pydantic needs 2.11 or later. A plan
  caches `__pydantic_validator__`, so a later `model_rebuild(force=True)` isn't seen by a plan that is already built.
- **Types are not checked at runtime**, except by pydantic. A class whose annotations don't match its columns fails
  only in `rowx check`.

## Execution limits

- **No multi-statement execution.** A query is one statement. Postgres prepares one statement at a time, so a string
  with several statements fails at prepare time. Run them as separate queries.
- **No `command` tag in `QueryResult`.** `QueryResult` carries `rows_affected` only. The tag (`"INSERT 0 5"`) is not
  exposed by the driver.
- **Extension types are verified once per statement.** The owning extension of `citext`, `hstore`, `ltree`, `lquery` and
  `cube` is checked when a statement is prepared. `DROP EXTENSION` or `ALTER EXTENSION` while a connection holds a
  cached statement is not noticed until that statement is prepared again.

## SQLite limits

The v1 SQLite backend leaves these out. Each is a decision, not an oversight.

- **No system `libsqlite3`.** SQLite is bundled ([driver reference](reference/driver.md#sqlite)), so the version is the
  one rowx ships.
- **No shared-cache mode**, no `ATTACH`-aware checking, no extension loading, no user-defined functions, no SQLCipher,
  no incremental blob IO, no update hooks, and no free-threaded build.
- **No sync API.** The driver is asyncio only.
- **No typed describe for virtual tables and FTS.** A virtual table or a table-valued function is `Unproven`, and its
  columns are untyped.
- **No `JSONB`.** A `JSONB` blob raises `DecodeError`. Store JSON as TEXT.
- **No WAL on a network filesystem.** A file database is opened in WAL mode, which needs shared memory.
- **No multi-statement execution at runtime** ([#300](https://github.com/mplemay/rowx/issues/300)). A query is one
  statement, and `execute` is also refused for a statement that returns rows.
- **Nullability is proven by a rule and the bytecode.** A `NOT NULL` column of a plain table read through `FROM` and
  inner joins is NOT NULL. Any other column, an expression, a view, CTE, subquery, compound select, outer join or
  aggregate, is NOT NULL only when the `EXPLAIN` bytecode proves it ([Nullability](reference/nullability.md#sqlite)).
  `count(*)` and `coalesce(b, 'z')` are proven; `max(a)`, a division, an unlisted function, and every column of a
  statement with a window function, a virtual table or a trigger stay `Unproven`. `"col!"` narrows it.
- **A compound column with mixed types is untyped.** SQLite declares a compound select's column, also inside a view,
  subquery or CTE, with its leftmost arm. rowx keeps that declaration only when every arm agrees, and otherwise checks
  the column as untyped, so `query[int]` over `SELECT i ... UNION ALL SELECT tx ...` is not reported and a text row
  raises `DecodeError` when read. A view column, and a compound over a `WITHOUT ROWID` table, is also untyped when the
  statement has a window function, a virtual table or another construct the bytecode analysis cannot model.
- **No session state at check time.** The checker describes against the database schema alone. A `TEMP` table or an
  `ATTACH`ed database created by the application at runtime is not seen, and a `TEMP` table that shadows a `main`
  table of the same name is checked as the `main` one. Qualify the name (`main.t`, `temp.t`) or avoid shadowing.
- **Parameters are untyped.** There is no `param-type-mismatch`, and a naive or aware `datetime` mismatch surfaces as a
  `DecodeError` when read ([Type mapping](reference/type-mapping.md#sqlite)).
- **`DECIMAL` and `JSON` columns are lossy** through SQLite's numeric affinity, and there is no column type override
  ([#302](https://github.com/mplemay/rowx/issues/302)).
- **One writer at a time**, and a cancel during a lock wait ends after the busy timeout.
- **A `-- no-transaction` migration is not locked.** A second migrator that starts meanwhile fails `Dirty`.

## Migration limits

- **`-- no-transaction` is still one implicit transaction.** The file is sent as a single simple-protocol query, so
  Postgres runs a multi-statement file as one transaction block. A statement that can't run in one
  (`CREATE INDEX CONCURRENTLY`, `DROP INDEX CONCURRENTLY`, `REINDEX ... CONCURRENTLY`, `VACUUM`) must be the only
  statement in its file, and an enum value from `ALTER TYPE ... ADD VALUE` can't be used in the same file unless the
  file commits in between with its own `BEGIN` and `COMMIT`.
- **PgBouncer in transaction mode is unsupported.** The migration lock is a session-level advisory lock, which a
  transaction-mode pooler doesn't keep. Migrate over a direct or session-mode connection.
- **A file that ends rowx's transaction is not detected.** A transactional file containing `COMMIT` or `ROLLBACK` runs,
  but what follows it is outside the transaction rowx opened, and the history row is written after the fact. Mark such
  files `-- no-transaction`.
- **Migrations don't set timeouts.** `statement_timeout` and `lock_timeout` come from the role, the database or the URL.
  `SET LOCAL` works in a transactional file but can't share a file with `CREATE INDEX CONCURRENTLY`.
- **A `SET` in a file outlives it on a `Connection`.** A session-level `SET` (such as `search_path`) stays on that
  connection after the run, until a pool's reset clears it. Use `SET LOCAL`, or a schema-qualified `table`.
- **A failed run reports no partial progress.** `run` returns nothing when it raises, so the `Applied` lines of
  migrations that committed before the failing one are not printed. `rowx migrate info` shows the state.
- **`rowx check --scratch` and `rowx.testing` execute migration files.** `rowx check` does so only with `--scratch`,
  in the database it creates, and the plugin in its template databases. Migration files are trusted code.
- **The test plugin is asyncio only.** Trio is unsupported, and roles created by a test are cluster-wide, so a database
  clone doesn't isolate them.

Rollout compatibility covers discoverable query sites and schema effects the checker can describe. It does not
execute queries or verify production data or permissions. For example, PostgreSQL 18.3 accepts Parse/Describe and
EXPLAIN for an INSERT omitting a new NOT NULL column with no default, even though execution fails.

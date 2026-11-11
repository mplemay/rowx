# Checker

`rowx check` finds every `query[T]` and `query_unchecked[T]` built from `rowx` or `rowx_core`, prepares each checked
statement against a live Postgres 18 database, describes it, and compares the result with `T`. It never imports or
executes application code. A query is checked at its construction site, including a declaration nothing calls.
Runtime compilation uses the executor you pass. The checker does not infer that flow.

Both public import roots are checked: `from rowx import query` and `from rowx_core import query`.

## CLI

```text
rowx check [PATHS...]
  [--database-url URL] [--config FILE] [--output-format text|json]
  [--statistics] [--inspect] [--coverage]
  [--require-checked] [--disallow-unchecked]
  [-j N] [--scratch] [--no-drift-check]
```

The entry point is `rowx = "rowx.check.cli:main"`.

| Exit code | Meaning |
|---|---|
| 0 | No diagnostics. That includes a run that found no queries. |
| 1 | One or more diagnostics. |
| 2 | Bad invocation or config, an unreachable database, a describe failure that is not an SQL error, or an internal error. |

A missing database URL is exit 2 only when at least one statement would be described. A file whose calls are all
`query_unchecked` does not need one.

`--inspect` prints each described statement's columns, nullability and parameter types. It does not execute the queries.
`--coverage` lists recognized direct query sites and whether each was described and compared. `--require-checked` adds
`coverage-require-checked` when no query was successfully described. `--disallow-unchecked` adds `coverage-unchecked`
for every recognized `query_unchecked` call. `--statistics` prints the scan counts, including
`query_unchecked calls skipped`, which counts calls whose SQL was not described. `-j` defaults to
`min(4, os.cpu_count() or 1)` and must be at least 1. `--scratch` and `--no-drift-check` are under
[Drift gate and scratch](#drift-gate-and-scratch).

With `--output-format json`, diagnostics stay on stdout. Statistics, coverage and inspect go to stderr.

`rowx check-compat --against BASE --head HEAD` checks the same sources at two revisions and reports a change in
column count, column name, column type or parameter types. The URL is `--database-url` or `ROWX_DATABASE_URL`. It
does not read `database_url_env`.

## Config

`pyproject.toml`:

```toml
[tool.rowx]
database_url_env = "DATABASE_URL"
include = ["src", "tests"]
exclude = ["**/generated/**"]
statement_timeout = "10s"

[tool.rowx.migrate]
dir = "migrations"
table = "_rowx_migrations"
```

| Key | Meaning |
|---|---|
| `database_url_env` | Environment variable holding the URL. Default `DATABASE_URL`. |
| `include` | Directory or file roots, relative to the config file. Default `["."]`. |
| `exclude` | Extra glob patterns, matched on top of the defaults below. A pattern with no slash matches a name at any depth. An absolute pattern matches nothing. |
| `statement_timeout` | A positive integer plus `ms`, `s` or `min`. Default `10s`. |
| `migrate` | Present or absent as a table. Its keys are `dir` (default `migrations`), `table` (default `_rowx_migrations`), `create_schemas`, `ignore_missing`, `allow_out_of_order`, `versioning` (`inferred`, `timestamp`, `sequential`) and `kind` (`inferred`, `simple`, `reversible`). |

There is no `check_search_path`, `target_version` or `explain` key. The checker session's `search_path` is whatever
the database URL already sets. Unknown keys are a config error (exit 2), with a close-match hint.

URL precedence is `--database-url`, then the configured environment variable. The implementation is
`src/rowx/check/config.py`.

Default exclude names, always applied, are `.git`, `.venv`, `venv`, `node_modules`, `__pycache__`, `site-packages`,
`build` and `dist`.

The config file is the nearest `pyproject.toml` walking up from the working directory, or `--config`. Tracked and
untracked `.py` files come from `git ls-files` when the path is inside that project and git succeeds. Otherwise the
directory is walked. Gitignored files are not checked. `.pyi` files are not checked.

## Discovery

`src/rowx/check/discover.py` walks source. It does not import the application.

- A call is a candidate when the callee resolves to `query` or `query_unchecked` on `rowx` or `rowx_core`, including
  `import rowx` / `import rowx_core` and aliases. A relative import is not. `from rowx import *` and
  `from rowx_core import *` are reported as an indirect use.
- Resolution is lexical and best-effort. A name that is also bound another way in the same scope is an ambiguous
  indirect use, and that call is not described.

### Call shape

- `query[T](t"…")`, `query(t"…")` and `query[None](t"…")` are checked when the argument is an inline t-string. A
  missing argument, a keyword, a star, or any non-t-string (including a name bound to a t-string in the same scope)
  is `dynamic-query`. A conversion, a format spec or a nested t-string in that literal is `dynamic-query` as well.
- `query_unchecked[T](t"…")` does not describe the SQL. `T` is still resolved, and a bad row type is
  `invalid-row-type` or `unresolved-row-type`. A non-literal format spec on an inline t-string is `dynamic-query`,
  because it could evaluate to `ident`. The printed count of `query_unchecked` calls is the number whose SQL was not
  described.
- A file whose calls are all unchecked does not need a database URL. Its row types are still resolved. The type
  vocabulary is loaded without opening a connection.
- A wrapper the walk cannot follow, and `query` or `query_unchecked` used as a value, is `indirect-query`.

What the walk cannot see stays silent: a callee it cannot resolve, SQL built in another module and passed in, and a
query inside `exec`. The checker does not claim otherwise.

`template-construction` (`src/rowx/check/construction.py`) runs on every discovered file, with or without a `query`
import. It reports a `Template(...)` call whose arguments are not literals, an indirect use that hides that call, and
a non-literal format spec in a t-string. The spec on a direct `query_unchecked` argument is `dynamic-query` instead,
so one interpolation is not reported twice. There is no suppression comment. Exclude the path, or build the SQL by
nesting t-string literals. The runtime behaviour is in
[Parameters and SQL text](security.md#parameters-and-sql-text).

## Source map

`src/rowx/check/sourcemap.py` turns a server `position` into a span in the user's file. A server position is a
1-based character index into the SQL the PostgreSQL compiler would send. The same compiler emits that SQL for
runtime execution and for this map.

`t"SELECT {a}, {b} FROM t"` compiles to `SELECT $1, $2 FROM t`. The characters of `SELECT`, including the space
after it, correspond. `$1` occupies
the span of `{a}`.

Assembly walks the t-string, records each interpolation's source span, and concatenates static text and
placeholders. `packages/rowx-postgres/src/rowx_postgres/_compiler.py` emits `$n` and the adjacency padding. The map
consumes `compiled.padding`. It does not invent a second spacing rule. An offset that lands on inserted padding is
attributed to the neighbouring interpolation.

`:ident` is recognised while assembling a `query_unchecked` template. The source span is the interpolation. Nested
templates are flattened in source order, and a cycle is `dynamic-query`. A template that is not an inline literal
has no spans, so the diagnostic points at the call. The file's encoding cookie is honoured. A file that does not
decode is `syntax-error`.

## Row-type resolution

`T` is an expression. `src/rowx/check/resolve.py` resolves it without importing the module. The grammar is the
[Row-type grammar](api.md#row-type-grammar).

The scope is module-level assignments and `type` aliases, class bodies, and function-local assignments when the call
is in that function. An imported name is found through the package layout and the include roots, not through
`sys.path`. A module outside the project is `unresolved-row-type`. `TYPE_CHECKING` blocks are read for imports. A
name assigned both inside and outside the block uses the runtime assignment. A name that exists only under
`TYPE_CHECKING` is `invalid-row-type` when it is used as a value the runtime would have to import.

Supported forms are `tuple[...]`, a scalar leaf, `X | None`, `Annotated`, `ReadOnly`, `Required`, `NotRequired`,
`type` aliases, and the named forms in [Named row types](api.md#named-row-types). A generic alias is substituted
from its parameters. A string annotation inside a class is parsed. A string as `T` itself is `invalid-row-type`.

`query_unchecked` uses this resolution even though its SQL is not described.

## Rules

| Rule | Meaning |
|---|---|
| `syntax-error` | The file did not parse, did not decode, or is nested too deeply to analyse. |
| `indirect-query` | A use the checker will not follow, including a star import of `rowx` or `rowx_core`. |
| `dynamic-query` | The SQL is not an inline t-string the checker can describe, or a `query_unchecked` interpolation has a non-literal format spec. `T` is still resolved. |
| `template-construction` | A hand-built `Template`, or a non-literal format spec, in a discovered file. |
| `unresolved-row-type` | A name in `T` could not be resolved. This includes a `query_unchecked` row type. |
| `invalid-row-type` | `T` resolved but is not in the grammar. This includes a `query_unchecked` row type. |
| `sql-error` | Postgres rejected the statement. The span is the mapped server position. |
| `param-count-mismatch` | The number of described parameters differs from the number of interpolations. |
| `missing-row-type` | The query returns columns and the row type is `None`. |
| `unexpected-row-type` | The query returns no columns and the row type is not `None`. |
| `arity-mismatch` | A tuple, a scalar, or a `NamedTuple` has a different column count. |
| `missing-column` | A field without a default has no column. |
| `unexpected-column` | A name-matched row has no field for a column. |
| `duplicate-column` | Two columns have the same stripped name, or two names of one field. |
| `ambiguous-column` | One column matches two pydantic fields. |
| `column-name-mismatch` | A `NamedTuple` column name differs from the field. |
| `unsupported-type` | The column's type has no leaf in the [type table](../types.md). |
| `type-mismatch` | A described type is not acceptable for the element. The message names both types. |
| `nullability-mismatch` | A column may be NULL and the element is not nullable. |
| `invalid-override` | A quoted alias of 63 bytes or more ends in `!` or `?`. |
| `param-type-mismatch` | The interpolation's evident type is not in the parameter's accepted types. `None` is accepted for every parameter. An unknown or unsupported parameter is not reported. |
| `coverage-require-checked` | `--require-checked`, and no query was successfully described. |
| `coverage-unchecked` | `--disallow-unchecked`, and the call is `query_unchecked`. |

Every rule is an error. Warnings, ignore comments and per-rule configuration are
[#296](https://github.com/mplemay/rowx/issues/296). `--fix` is
[#301](https://github.com/mplemay/rowx/issues/301).

Type compatibility is nominal, with two relaxations. A domain is acceptable wherever its base is. `int2`, `int4`
and `int8` all accept `int`, and `float4` and `float8` both accept `float`. Anything else is `type-mismatch`,
including `int` for a `numeric` column. `None`, `Any` and `object` silence `nullability-mismatch` for that column.
They do not silence `type-mismatch`.

A template with more than one statement is `sql-error` from Postgres. A parameter in a DDL statement is a server
error.

## Output

Text is the default. One block per diagnostic, in file order then source position:

```text
error[type-mismatch]: column 2 is `int8` but the row type expects `str`
  --> src/app.py:14:18
   |
14 |     query[tuple[int, str]](t"SELECT id, name FROM users")
   |                  ^^^
   |
   = note: …
```

Notes and help lines follow the snippet. Tabs expand to four spaces. A zero-width span still gets one caret.
Server-supplied text is sanitised before it reaches a terminal ([Checker](security.md#checker)).

`--output-format json` prints one JSON array. Each object has `rule`, `message`, `file`, `line`, `col`, `end_line`,
`end_col`, `notes`, `help`, `sqlstate`, `sql`, `column_index` and `field`. There is no separate footer. Pass
`--statistics` for the counts.

Described column metadata is not written to a disk cache. Every check prepares the statements it describes.

## Migrate and database commands

`rowx migrate add|run|revert|skip|info` and `rowx database create|drop|reset|setup` are the CLI over
[Migrations](migrations.md). `add` writes a migration file and needs no database and no backend, including when the
directory already exists. The other migrate commands need a backend. `database` commands resolve a backend from the
URL. `reset` and `setup` load the migrator before they change the database, so an invalid migration directory fails
before a drop. `drop` does not read the migration files.

## Drift gate and scratch

The drift gate runs when `[tool.rowx.migrate]` is present, `--no-drift-check` is absent, and `--scratch` is not set.
It compares the migration files with the history table using the `info` plan, which takes no lock and creates
nothing. The connection is read-only (`migrator.open(readonly=True)`). It does not apply migrations. A pending,
modified or missing migration fails the command. `ignore_missing` lets a missing migration pass. Implementation:
`src/rowx/check/drift.py`.

`--scratch` needs `[tool.rowx.migrate]`. It treats the URL as a server URL, creates a temporary database, applies
the migrations, and checks against that. The temporary database is dropped at the end, including on cancellation.
The connecting role needs `CREATEDB`. The drift gate does not run in this mode. Implementation:
`src/rowx/check/scratch.py`.

## Module map

| Module | Role |
|---|---|
| `src/rowx/check/cli.py` | Argument parsing, policies and exit codes. |
| `src/rowx/check/config.py` | `pyproject.toml` and URL precedence. |
| `src/rowx/check/discover.py` | Import tracking and call extraction. |
| `src/rowx/check/walk.py` | File discovery. |
| `src/rowx/check/sourcemap.py` | Template assembly and position mapping. |
| `src/rowx/check/resolve.py` | Row-type resolution. |
| `src/rowx/check/compare.py` | Column and parameter comparison. |
| `src/rowx/check/params.py` | Placeholder and parameter types. |
| `src/rowx/check/render.py` | Text and JSON output. |
| `src/rowx/check/runner.py` | Scheduling and the checker session. |
| `src/rowx/check/model.py` | Diagnostic and rule types. |
| `src/rowx/check/migrate.py` | `rowx migrate`. |
| `src/rowx/check/database.py` | `rowx database`. |
| `src/rowx/check/drift.py` | Drift gate. |
| `src/rowx/check/scratch.py` | `--scratch`. |
| `src/rowx/check/construction.py` | `template-construction`. |
| `src/rowx/check/compat.py` | `rowx check-compat`. |

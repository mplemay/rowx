# Checker

`rowx check` finds every `query[T]` and `query_unchecked[T]` built from `rowx` or `rowx_core`, prepares
each checked statement against a live Postgres 18 database, describes it, and compares the result with `T`. It never
imports or executes application code. A query is checked at its construction site, including a declaration nothing
calls. Runtime compilation uses the executor you pass. The checker does not infer that flow.

The checked import roots are `from rowx import query` and `from rowx_core import query`.

## CLI

```text
rowx check [PATHS...]
  [--database-url URL] [--config FILE] [--output-format text|json]
  [--statistics] [--inspect] [--coverage]
  [--error RULE] [--warn RULE] [--ignore RULE] [--exit-zero-on-warning]
  [--respect-ignore-files | --no-respect-ignore-files] [--force-exclude]
  [--require-checked] [--disallow-unchecked]
  [-j N] [--scratch] [--no-drift-check]
```

The entry point is `rowx = "rowx.check.cli:main"`.

| Exit code | Meaning |
|---|---|
| 0 | No visible diagnostics, including a run that found no queries; also warnings only when `--exit-zero-on-warning` or `error-on-warning = false` is set. |
| 1 | One or more visible errors, or warnings when warnings fail the check (the default). |
| 2 | Bad invocation or config, an unreachable database, a describe failure that is not an SQL error, a drift or scratch failure, or an internal error. |

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

`rowx check-compat --against BASE --head HEAD` checks the same sources at two revisions. Every cross-schema report with
`not report.wider_nullability` is incompatible. That includes a change in column count, column name, column type or
parameter types, and a NOT NULL column becoming nullable against a non-null annotation. A wider annotation (`X | None`
on a NOT NULL column) is not incompatible. Exit 0 is a complete compatible subset, exit 1 is an incompatibility, and
exit 2 is incomplete: `incomplete: loading revision`, `incomplete: building schema`, own-schema coverage was not
established, a comparison could not be established, or scratch cleanup failed. Compatibility uses raw checker
evidence: rule levels and inline suppressions cannot turn an incompatible or incomplete check into a compatible one.
Snapshot configuration still validates rule settings and applies its versioned path exclusions; snapshot discovery
does not apply `.gitignore` or `.ignore`. The loading-revision and building-schema failures are `CheckError`s raised
before the 0/1/2 return. The URL is `--database-url` or `ROWX_DATABASE_URL`. It does not read `database_url_env`.
An `embedded://` URL builds each revision's schema in memory, with one instance per describe worker, instead of a
scratch database on a server, and prints no disposable-server warning; an embedded directory URL is a `CheckError`
(exit 2). See [Embedded Postgres](embedded.md#checker).

## Config

`pyproject.toml`:

```toml
[tool.rowx]
database_url_env = "DATABASE_URL"
include = ["src", "tests"]
exclude = ["**/generated", "!src/generated/keep.py"]
statement_timeout = "10s"
respect-ignore-files = true
force-exclude = false

[tool.rowx.rules]
all = "error"
invalid-ignore-comment = "warn"
unused-ignore-comment = "warn"

[tool.rowx.terminal]
error-on-warning = true

[tool.rowx.migrate]
dir = "migrations"
table = "_rowx_migrations"
```

| Key | Meaning |
|---|---|
| `database_url_env` | Environment variable holding the URL. Default `DATABASE_URL`. |
| `include` | Project-root-anchored directory or file patterns. Default `["."]`. |
| `exclude` | Ordered project-root-anchored patterns applied after the default exclusions; a leading `!` reincludes a match. Default `[]`. |
| `respect-ignore-files` | Respect `.gitignore` and `.ignore` during ordinary discovery. Default `true`; `--respect-ignore-files` and `--no-respect-ignore-files` override it. |
| `force-exclude` | Apply filters to explicitly supplied paths. Default `false`; `--force-exclude` enables it. |
| `rules` | Table mapping rule names or `all` to `error`, `warn` or `ignore`. See [Rules](#rules). |
| `terminal` | Table with `error-on-warning` (boolean, default `true`). `--exit-zero-on-warning` makes warnings alone exit 0. |
| `statement_timeout` | A positive integer plus `ms`, `s` or `min`. Default `10s`. |
| `migrate` | Present or absent as a table. Its keys are `dir` (default `migrations`), `table` (default `_rowx_migrations`), `create_schemas`, `ignore_missing`, `allow_out_of_order`, `versioning` (`inferred`, `timestamp`, `sequential`) and `kind` (`inferred`, `simple`, `reversible`). |

There is no `check_search_path`, `target_version` or `explain` key. The checker session's `search_path` is whatever
the database URL already sets. Unknown keys are a config error (exit 2), with a close-match hint.

URL precedence is `--database-url`, then the configured environment variable. The implementation is
`src/rowx/check/config.py`.

The config file is the nearest `pyproject.toml` walking up from the working directory, or `--config`. Patterns are
relative to its directory (the project root); without a config file the working directory is the root. `src` matches
only root `src`, while `**/src` matches that name at any depth. A matching directory includes its descendants. `*`
and `?` match within a path component, `**` is a whole component, and character classes and ranges and backslash
escapes are supported. A trailing slash restricts a match to a directory; optional leading `./` or `/` anchors at the
root. Invalid patterns, including an unclosed class, trailing escape or embedded `**`, are config errors. Include
patterns select directories or `.py` files; unmatched glob includes select nothing, while a missing literal path is
an error. Results are deduplicated and sorted. To migrate an old basename exclusion at any depth, change, for example,
`generated` to `**/generated`.

The defaults exclude `.venv`, `venv`, `node_modules`, `__pycache__`, `site-packages`, `build` and `dist` at any
depth. A later negated exclude can reinclude these directories or descendants. `.git` is a hard exclusion at any
depth, including explicitly selected directories, and cannot be reopened.

Ordinary discovery respects nested `.gitignore` and `.ignore` even outside Git. Each file is relative to its own
directory and follows gitignore comment, anchoring, directory and negation rules. `.ignore` takes precedence over
`.gitignore` in the same directory; deeper ignore files override higher files, but a child cannot reopen a parent
excluded by an ignore file unless that parent is reopened. Ancestor ignore files up to the project root are read.
Tracked status does not override these rules: tracked and untracked files are selected alike. Unreadable or invalid
UTF-8 ignore files and directory read failures are configuration errors (exit 2). No global Git excludes are used.

A path supplied explicitly on the CLI bypasses include, exclude and ignore-file filtering for that path itself unless
`--force-exclude` or `force-exclude = true` is set. Explicit files must still have the `.py` suffix. An explicit
directory bypasses exclusions of the selected directory and its ancestors, while independently matching rules still
filter descendants. Configured include entries and glob matches are not explicit CLI paths. Directory symlinks,
including symlink ancestors of configured includes, are not traversed; an explicitly selected CLI file may still
use such a path. External explicit paths are supported; their filtering is anchored at the selected directory or, for
a file, its parent. Filtering selects files for checking; it does not change runtime imports or type resolution.

## Discovery

`src/rowx/check/discover.py` walks source. It does not import the application.

- A call is a candidate when the callee resolves to `query` or `query_unchecked` on `rowx` or `rowx_core`, including
  `import rowx` / `import rowx_core` and aliases. A relative import is not. `from rowx import *` and
  `from rowx_core import *` are reported as an indirect use. `rowx.core` does not export `query`.
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
so one interpolation is not reported twice. The rule can be configured or suppressed like other ordinary rules.
The runtime behaviour is in
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
templates are flattened in source order. A repeated nested template is shared. A non-inline argument of `query` is
`dynamic-query` on that argument (`query[int](sql)` spans `sql`). The same rule is reported on the call when the
argument count, a keyword, or a star means it is not exactly one positional argument. If the argument of
`query_unchecked` is not an inline t-string, there is no diagnostic. If it is an inline t-string, the SQL is still
not described, a non-literal format spec is `dynamic-query`, and `T` is still resolved. The file's encoding cookie is
honoured. A file that does not decode is `syntax-error`.

## Row-type resolution

`T` is an expression. `src/rowx/check/resolve.py` resolves it without importing the module. The grammar is the
[Row-type grammar](api.md#row-type-grammar).

The scope is module-level assignments and `type` aliases, class bodies, and function-local assignments when the call
is in that function. An imported name is found through the package layout and the include roots, not through
`sys.path`. For each include pattern the source root is its literal prefix before the first wildcard; a file
prefix uses its parent, and only existing directories are used. The directory containing the checked file’s
top-level package is also a root. Names from `KNOWN_ROOTS` (the standard library, `typing_extensions`, `rowx`,
`rowx_core`, `pydantic`,
plus the roots of a selected backend's value-class modules) that are not a supported row type or base are
`invalid-row-type`. A root outside that set is outside the project and is `unresolved-row-type`. `TYPE_CHECKING`
blocks are read for imports. A name assigned both inside and outside the block uses the first `TYPE_CHECKING`
binding. A name that exists only under `TYPE_CHECKING` is `invalid-row-type` in a field annotation and in an alias
body, with help `import X at runtime`. A `query[T]` uses the `TYPE_CHECKING` binding, so `from datetime import date`
inside that block can resolve.

Supported forms are `tuple[...]`, a scalar leaf, `X | None`, `Annotated`, `ReadOnly`, `Required`, `NotRequired`,
`type` aliases, and the named forms in [Named row types](api.md#named-row-types). A generic alias is substituted
from its parameters. A string annotation inside a class is parsed. A string as `T` itself is `invalid-row-type`.

`query_unchecked` uses this resolution even though its SQL is not described.

## Rules

| Rule | Meaning |
|---|---|
| `syntax-error` | The file did not parse, did not decode, or is nested too deeply to analyse. |
| `indirect-query` | A use the checker will not follow, including a star import of `rowx` or `rowx_core`. |
| `dynamic-query` | For `query`, a non-inline argument (reported on that argument) or a call that is not exactly one positional argument because of the argument count, a keyword, or a star (reported on the call). An inline `query` t-string with a conversion, a format spec, or a nested t-string. If the argument of `query_unchecked` is not an inline t-string, there is no diagnostic. If it is an inline t-string, the SQL is still not described, a non-literal format spec is this rule, and `T` is still resolved. |
| `template-construction` | A hand-built `Template`, or a non-literal format spec, in a discovered file. |
| `unresolved-row-type` | A name in `T` could not be resolved. This includes a `query_unchecked` row type. |
| `invalid-row-type` | `T` resolved but is not in the grammar. This includes a `query_unchecked` row type. |
| `sql-error` | Postgres rejected the statement. The span is the mapped server position. |
| `param-count-mismatch` | The number of described parameters differs from the number of interpolations. |
| `missing-row-type` | Columns remain after `void` columns are dropped, and the row type is `None`. An all-`void` statement with row type `None` has no diagnostic. |
| `unexpected-row-type` | No columns remain after `void` columns are dropped, and the row type is not `None`. `compare` is not called. |
| `arity-mismatch` | A tuple, a scalar, or a `NamedTuple` has a different column count. |
| `missing-column` | A field without a default has no column. |
| `unexpected-column` | A name-matched row has no field for a column. |
| `duplicate-column` | Two columns have the same stripped name, or two names of one field. |
| `ambiguous-column` | One column matches two pydantic fields. |
| `column-name-mismatch` | A `NamedTuple` column name differs from the field. |
| `unsupported-type` | A column or a parameter whose type has no leaf in the [type table](../types.md). |
| `type-mismatch` | A described type is not acceptable for the element. The message names both types. |
| `nullability-mismatch` | A column may be NULL and the element is not nullable, or a NOT NULL column faces an element that is a union with `None`. The second finding is checker-only: the runtime NULL check does not raise for it. |
| `invalid-override` | The described name is longer than 59 bytes and at most 63, and a quoted source alias is longer than 63 bytes, ends in `!` or `?`, and starts with that name. An alias of exactly 63 bytes that still ends in the suffix is not reported. A described name of 59 bytes or shorter is not reported. |
| `param-type-mismatch` | An evident Python type the parameter does not accept. `None` is accepted for every parameter. It is not emitted for an unknown type or for a parameter already reported as `unsupported-type`. |
| `coverage-require-checked` | `--require-checked`, and no query was successfully described. |
| `coverage-unchecked` | `--disallow-unchecked`, and the call is `query_unchecked`. |
| `invalid-ignore-comment` | A malformed rowx ignore or an unknown rule in one. Default `warn`. |
| `unused-ignore-comment` | An ignore or individual rule entry did not suppress a visible diagnostic. Default `warn`. |

All other rules default to `error`. Set `[tool.rowx.rules]` entries to `error`, `warn` or `ignore`; `all` applies
first and specific entries override it. Repeat `--error RULE`, `--warn RULE` and `--ignore RULE` to override config
in command-line order; each accepts a rule name or `all`, and the last applicable option wins. Unknown names, levels
or malformed tables are configuration errors. Warnings cause exit 1 by default; set
`[tool.rowx.terminal] error-on-warning = false` or pass `--exit-zero-on-warning` for warnings alone to exit 0.

Use a native Python comment to suppress an ordinary diagnostic:

```python
from rowx import query

query["Row"](t"SELECT 1")  # rowx: ignore[invalid-row-type] # reason
```

A directive on its own line before the first Python code token applies to the whole file. Blank lines, shebangs and
encoding comments may precede it. A standalone directive after code has begun has no next-line or block effect. A
trailing directive applies when its line is the first or last occupied line of a diagnostic's primary span; a middle
line alone does not match. Spans use end-exclusive positions, so an end at column 1 of a later line belongs to the
preceding line. Zero-width spans remain on their own line. A bare `# rowx: ignore` suppresses ordinary rules, while
bracketed directives name exact lowercase rules, separated by commas. Whitespace around the colon and codes is
accepted. An optional reason follows whitespace and a further `#`. Multiple `#`-delimited tool comments can share a
line. Only Python comment tokens count; directive text in strings does not.

Malformed syntax, empty lists and unknown names produce `invalid-ignore-comment`; a malformed bracketed directive
never becomes a broad ignore. Valid names in a mixed valid/unknown list still work. Rule levels are applied before
usage accounting, so disabled findings cannot make an ignore used. Each visible finding is consumed by the most local
matching directive (inline before file-wide, then source order); one entry may suppress multiple findings. Each
unmatched or redundant valid rule entry produces an unused finding, as does a broad ignore with no effect. A bare
ignore cannot suppress `unused-ignore-comment`; name that rule explicitly to suppress it. This second pass does not
create recursive unused findings for that entry. If tokenization fails, no ignores or unused findings from its partial
stream are applied; an undecodable source cannot be suppressed. A parse error can be suppressed when tokenization
succeeds.

Suppression and ignored rule levels change reporting, not discovery, SQL description, inspection or coverage evidence;
a suppressed query can still require a database connection. Operational failures retain their provenance and stay
visible as exit 2 even if their rule is `sql-error` or configured to ignore; ordinary PostgreSQL SQL errors remain
suppressible. `type: ignore`, `noqa` and `@no_type_check` do not suppress rowx SQL checks. Generic type-checker
suppression compatibility, baselines and automatic insertion of ignores are deferred. `--fix` is
[#301](https://github.com/mplemay/rowx/issues/301).

Type compatibility is nominal, with two relaxations. A domain is compared as its base type, so it is acceptable
wherever that base is. `int2`, `int4` and `int8` all accept `int`, and `float4` and `float8` both accept `float`.
`int` for a `numeric` column is `type-mismatch`.

`typing.Any` and `builtins.object` are the leaf `any`. They are the only annotations neutral to both type and
nullability. Once a column is supported and is not `void`, comparison returns no findings, so they match that
column's type and either nullability. An unsupported column is still `unsupported-type`. `_row_reports` drops
columns with `type_key == "void"` before it decides. If none remain and the row type is `None`, there is no
diagnostic. If none remain and the row type is anything else, the rule is `unexpected-row-type` and `compare` is
not called. `type-mismatch` for `void`, including `Any` and `object`, happens only when `compare` runs because
some other column is not `void`. Then a `void` column matches only the element `None` (leaf `none`), including a
named field typed `None`. Anything else facing that `void` column is `type-mismatch`, and the help says to use
`.execute()`. A bare `None` element against any other type is `type-mismatch` too. It does not silence nullability.

`json` and `jsonb` accept any leaf annotation other than `None`. Nullability is still checked, unless the element
is `Any` or `object`.

An array matches `list[...]` when the innermost leaf is the element type. The annotation does not record
dimensions, so any nesting depth is accepted. An element may be `X` or `X | None` at every level, and that
`| None` is not a nullability mismatch. The array column's own nullability is still checked against whether the
annotation is `list[...] | None`.

A template with more than one statement is `sql-error` from Postgres. A parameter in a DDL statement is a server
error.

## Parameter type inference

`src/rowx/check/params.py` infers a type for each interpolation. Comparison then uses the type table's accepted
parameter types. An unknown type is not a `param-type-mismatch`. A parameter with no leaf is `unsupported-type`,
and that comparison does not also emit `param-type-mismatch`. The value `None`, and an annotation that is only
`None`, is accepted for every parameter. For an array parameter the nested lists are flattened to their leaf,
because the declared type does not record dimensions, and that leaf is checked against the element type. A leaf
that was not inferred is not reported.

**Literals.** An `int`, `float`, `str`, `bytes` or `bool` constant is that type, including a unary `+` or `-` on
an `int` or `float`. `True` is `bool`, not `int`. `None` is `None`. An inline t-string is `Template`. A list or
tuple literal is `list[...]` when every element has the same evident type. An empty list or tuple, a mix of
types, a `None` element beside another type, a star (`[1, *xs]`), or a name with no evident type is unknown. A
complex number is unknown.

**Constructors.** A call is evident only when its callee resolves, through imports, to one of these:

- `decimal.Decimal`, `uuid.UUID`, `datetime.date`, `datetime.datetime`, `datetime.time`, `datetime.timedelta`,
  `datetime.datetime.now` (including `datetime.now` after `from datetime import datetime`)
- `bytearray` and `memoryview`, including `builtins.bytearray` and `builtins.memoryview` when that is how they
  are written
- an `ipaddress` class (`IPv4Interface`, `IPv6Interface`, `IPv4Address`, `IPv6Address`, `IPv4Network`,
  `IPv6Network`)
- a `rowx_postgres` value class the backend records (`Money`, `Interval`, `Range`, `Cube`, and the geometric
  types)

`date.today()` is unknown. So is `int(1)`, `str(1)`, or any other builtin call. A shadowed builtin stays
unknown: a function named `bytearray`, or `Decimal` imported from anywhere other than `decimal`, is not the
constructor. A relative import does not resolve. `from module import *` makes the name unknown. `Range[int]` is
evident as `Range`. The type argument is not part of the inferred parameter type.

**Names.** A name is evident only when the scope that sees it has exactly one binding, and that binding is an
annotated parameter, an `AnnAssign`, or an `Assign` whose value is itself evident. An `AnnAssign` uses the
annotation, not the value, so `a: int = compute()` is `int`. An `Assign` follows the value, including a chain
`a = b = 1` and a name copied from another evident name. A self-reference (`x = x`) or a mutual reference is
unknown.

Anything else bound to that name in the same scope makes it unknown: a second assignment or annotation, an
`AugAssign`, `del`, a walrus, a `for` or `async for` target, `with … as`, an unpacking target, `except … as`, an
import, a function or class definition, a `type` alias, or a `match` capture. `*args` and `**kwargs` are unknown
even when annotated. `global` or `nonlocal` anywhere in the file makes that name unknown in every scope. A class
body name is visible only in that class body, not in its methods and not after the class. A comprehension target
does not hide the same name outside the comprehension. A walrus inside a comprehension is bound in the scope
around the comprehension, so it hides a name there.

**Annotations.** In an annotation, `X | None` counts as `X`, and `None` alone counts as `None`. A union of
distinct non-`None` types is unknown, including when one member does not resolve. `int | int` is `int`.
`list[int]` and `list[list[str]]` are evident. `list[int | None]` is `list[int]`. A bare `list`, a
`list[object]`, and a string annotation (`"int"`) are unknown. `dict[...]` is `dict`. A builtin used in an
annotation must not be shadowed. `from some_module import *` makes a builtin annotation unknown. A type parameter
that reuses a builtin name (`def f[int](x: int)`) hides that builtin inside the function.

## Output

Text is the default. One block per diagnostic, in file order then source position:

```text
error[type-mismatch]: column 2 "name": column type is `int` but the annotation is `str`
  --> src/app.py:14:22
   |
14 |     query[tuple[int, str]](t"SELECT id, name FROM users")
   |                      ^^^ element 2
```

Warnings use `warning[rule]` in place of `error[rule]`. Note lines are `= note:` and help lines are `= help:`. They
follow the snippet. The sample above stops after the caret. Tabs expand to four spaces. A zero-width span still gets one
caret. Server-supplied text is sanitised before it reaches a terminal ([Checker](security.md#checker)).

`--output-format json` prints one JSON array. Each object has `rule`, `severity` (`error` or `warning`), `message`,
`file`, `line`, `col`, `end_line`, `end_col`, `notes`, `help`, `sqlstate`, `sql`, `column_index` and `field`. There is
no separate footer. Pass `--statistics` for the counts; diagnostic counts reflect visible reports, while coverage
evidence is unaffected by reporting filters.

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
modified, missing, or dirty migration fails the gate. `ignore_missing` skips only a missing migration, so a dirty
migration still fails. Implementation: `src/rowx/check/drift.py`.

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
| `src/rowx/check/rules.py` | Rule levels and reporting policy. |
| `src/rowx/check/suppress.py` | Native ignore comments and usage accounting. |
| `src/rowx/check/migrate.py` | `rowx migrate`. |
| `src/rowx/check/database.py` | `rowx database`. |
| `src/rowx/check/drift.py` | Drift gate. |
| `src/rowx/check/scratch.py` | `--scratch`. |
| `src/rowx/check/construction.py` | `template-construction`. |
| `src/rowx/check/compat.py` | `rowx check-compat`. |

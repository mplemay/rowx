# Static checker (`rowx check`)

## CLI

```text
rowx check [PATHS...] [--database-url URL] [--config FILE] [--output-format text|json]
           [--statistics] [-j N] [--scratch] [--no-drift-check]
```

`rowx` also has the `migrate` and `database` commands ([Migrate and database commands](#migrate-and-database-commands)).
`rowx.check.cli:main` stays the entry point and builds the root parser.

- **Config.** `pyproject.toml` is found by searching upward from the current directory,
  unless `--config` is given. A missing `[tool.rowx]` table means defaults.
- **Paths.** Explicit `PATHS` are resolved relative to the current directory. They
  override `include` but still honour `exclude`.
- **Database URL.** It comes from `--database-url`, else the env var named by
  `database_url_env`.
- **Concurrency.** `-j` defaults to `min(4, os.cpu_count())`. Parsing and discovery run
  sequentially, and describes run concurrently over N connections, each with one operation
  in flight.
  - Describe results are cached per distinct SQL string, and diagnostics are reported per
    call site.
  - If a connection is lost mid-run (57P01 or 08xxx), the describe is retried once on a
    fresh connection; if that fails, the checker exits 2.
  - Ctrl-C cancels outstanding describes.
- **Output.** Diagnostics are sorted by (path, line, col, rule) before printing, so
  `-j 1` and `-j 8` produce byte-identical output.
- **Exit codes:**
  - **0:** no diagnostics, including when no queries were found;
  - **1:** one or more diagnostics;
  - **2:** a usage or config error, a missing or unreachable database, a describe failure
    that is not an SQL error (timeout 57014, connection loss), an internal error, or a
    Python older than 3.14.
- **Migrations.** When `[tool.rowx.migrate]` is configured, `check` gates on migration drift (`--no-drift-check` skips
  it), or builds a scratch database from the migrations (`--scratch`); see
  [Drift gate and `--scratch`](#drift-gate-and---scratch).
- There are no warnings, severities, ignore comments or `--fix`.
- **`--statistics`** reports:
  - files scanned and call sites found;
  - call sites checked, and distinct SQL strings described;
  - `query_unchecked` calls skipped;
  - diagnostics per rule.

  It goes to stdout in text mode and to stderr in JSON mode.

## Config (`pyproject.toml`)

```toml
[tool.rowx]
include = ["src"]                 # default ["."]
exclude = ["**/migrations/**"]    # always also: .git, .venv, venv, node_modules, __pycache__, site-packages, build, dist
database_url_env = "DATABASE_URL"
statement_timeout = "10s"         # integer + unit: ms|s|min
```

Unknown keys are an error (exit 2), with a did-you-mean suggestion; port the logic from
`sqlty/_config.py`. `.gitignore` is honoured through `git ls-files --others --exclude-standard
--cached`, with a fallback to the exclude globs when git isn't available.

```toml
[tool.rowx.migrate]
dir = "migrations"                 # relative to pyproject.toml
table = "_rowx_migrations"         # "_sqlx_migrations" adopts an existing sqlx project
create_schemas = []
versioning = "inferred"            # inferred | timestamp | sequential  (for `migrate add`)
kind = "inferred"                  # inferred | simple | reversible     (for `migrate add`)
```

`[tool.rowx.migrate]` is parsed in `check/config.py` with the same unknown-key and did-you-mean errors. Its presence
(even as an empty table) is what "configured" means for the drift gate. The semantics of `table` and `create_schemas`
are in [Migrations](migrations.md#history-table).

## Migrate and database commands

```text
rowx migrate add <description> [--reversible | --simple] [--timestamp | --sequential]
rowx migrate run    [--target-version V] [--dry-run] [--allow-out-of-order] [--ignore-missing]
rowx migrate revert [--target-version V] [--dry-run]
rowx migrate info
rowx database create | drop [--force] [-y] | reset [--force] [-y] | setup
```

The handlers are `check/migrate.py` (`migrate`) and `check/database.py` (`database`), so `rowx.check` is the CLI
package. Both import `rowx.postgres` and run the same [`Migrator`](migrations.md) and connection types as the runtime.

- **Database URL.** Every command finds it as `check` does: `--database-url`, else the variable named by
  `database_url_env`. `--config` and the upward search for `pyproject.toml` also work as for `check`. The DSN and
  password are never printed.
- **`migrate add`** creates the file or files in `dir`, creating the directory if needed, and needs no database.
  - *Versioning.* Unless a flag or `versioning` says otherwise, it is inferred by sqlx's rule: if the last two versions
    differ by 1, or the only version is 0 or 1, the new version is sequential (the last plus 1). Otherwise it is a
    timestamp. Timestamps are UTC `YYYYMMDDHHMMSS`.
  - *Kind.* Unless a flag or `kind` says otherwise, it is the kind of the latest migration (simple when there is none).
    `--reversible` writes `<VERSION>_<description>.up.sql` and `.down.sql`, and `--simple` writes one `.sql` file.
  - The description has spaces replaced with `_`.
- **`migrate run`, `revert` and `info`** print one line per migration, for example
  `Applied 20261029120000/migrate create users (12.3ms)`.
  - `run` maps to `Migrator.run`: `--target-version` is `target`, and the flags set `allow_out_of_order` and
    `ignore_missing`.
  - `revert` maps to `Migrator.undo`. With no `--target-version` it reverts only the latest migration; with one, it
    reverts every version above it, and `0` reverts all.
  - `--dry-run` prints the lines the command would print (with a `Would apply` or `Would revert` verb), changes nothing
    and creates no history table.
  - `info` prints one line per version with its state, from `Migrator.info`.
  - They exit **0** on success and **2** on any error (a `MigrateError`, a usage or config error, or an unreachable
    database), matching `check`'s exit-2 class. There is no exit 1.
- **`database`** connects to the **maintenance database**: `postgres`, or `template1` when the target database is itself
  named `postgres` (as `createdb` does). The target name comes from the URL, and names are quoted identifiers.
  - `create` creates the database. It is an error if it exists.
  - `drop` drops it, and `reset` drops and creates it. Both ask for confirmation on a TTY, and need `-y` otherwise.
  - `--force` uses `DROP DATABASE … WITH (FORCE)`, which disconnects other sessions.
  - `setup` runs `create` when the database is missing, then `migrate run`.
  - They exit 0 or 2 as above.

## Drift gate and `--scratch`

- **Drift gate.** When `[tool.rowx.migrate]` is configured, `rowx check` reads the history table inside its read-only
  session before describing anything. If any migration is pending, modified, missing or dirty, it exits 2 with a
  summary, for example: "database is 2 migrations behind `migrations/`: …; run `rowx migrate run` or
  `rowx check --scratch`". The read creates nothing: a missing history table counts as every migration pending.
  `--no-drift-check` skips the gate.
- **`--scratch`.** The configured URL is used as a **server** URL.
  - *Naming and creation.* The checker creates `rowx_check_<hex>` (`TEMPLATE template0`).
  - *Migrating.* It runs the migrations in that database as the connecting role.
  - *Checking.* It then opens its usual [read-only session](nullability.md#interface-and-checker-session) on that
    database for the run.
  - *Cleanup.* The scratch database is dropped `WITH (FORCE)` on **every** exit path, including errors and Ctrl-C.
  - *Requirements.* The role needs `CREATEDB`. The drift gate doesn't apply, because the database is built from the
    files.
  - If `[tool.rowx.migrate]` is not configured, `--scratch` is a usage error (exit 2).
- The checker's safety statement is amended for `--scratch`
  ([Interface and checker session](nullability.md#interface-and-checker-session)).

## Discovery (`check/discover.py`)

- Every file is read as bytes and decoded as UTF-8, honouring a BOM and any `# coding:`
  line. It is parsed with `ast.parse(source, filename)`, with no `feature_version`.
- A decode failure or `SyntaxError` is a `syntax-error` diagnostic at the reported
  position, and the file is skipped.

**Name resolution is lexical.**

- An import (`from rowx import query [as q]`, `import rowx [as rx]`) binds the name in the
  scope where it appears, whether that is module level, function level, under
  `if TYPE_CHECKING:` or inside `try:`. The innermost scope wins.
- If the name is also bound any other way in that scope (by assignment, a parameter, a
  `for` target, and so on), the name is ambiguous, and each use of it is `indirect-query`.

**Allowed uses** of a name that resolves to `rowx.query` or `rowx.query_unchecked`:

- (a) `NAME[T](arg)` or `NAME(arg)`, where the `Call`'s `func` is exactly that `Subscript`
  or `Name` (or `rx.query…` as an `Attribute`). Chaining (`….one(conn)`, `await …`)
  is irrelevant, because the inner `Call` is the match.
- (b) Annotation position, and `isinstance` or `issubclass` arguments.
- Any other use is `indirect-query`. That covers an assignment right-hand side
  (`Q = query[T]`), a call argument (`partial(query, …)`), a base class, a `return`
  value, `from rowx import *`, and `getattr(rowx, …)`.

**Call shape.**

- For `query`, the call must have exactly one positional argument and no keywords,
  starred arguments or `**`. That argument must be an inline `ast.TemplateStr`, optionally
  parenthesised. Anything else is `dynamic-query`.
- For `query_unchecked`, the call is counted and its SQL is skipped. Its `T` is still
  resolved, and is reported as `invalid-row-type`/`unresolved-row-type` when it fails.

**Interpolations in `query`.**

- A `conversion` other than -1 (this includes `{x=}`) is `dynamic-query`.
- Any `format_spec` is `dynamic-query`.
- An interpolation whose expression is an inline `TemplateStr`, or whose evident type
  ([Parameter type inference](#parameter-type-inference-checkparamspy-sqlty-style)) is `string.templatelib.Template`, is
  `dynamic-query`.
- Any other nested `Template` is caught only at runtime, as a `TypeError`.

### Source map (normative algorithm, `check/sourcemap.py`)

`ast` offsets alone aren't enough:

- They are UTF-8 byte columns.
- `Constant` spans cover raw source, including escapes and `{{`.
- Implicit concatenation merges pieces into one `Constant`, whose span includes the quote
  seams.
- CRLF line endings are normalised in values.

The algorithm:

1. **Read lines.** Keep each line's original terminator. A diagnostic's `line` is 1-based,
   and its `col` is a 1-based **Unicode code point** index on that line. JSON output uses
   the same units, with the end exclusive. Byte columns from `ast` are converted with
   `len(line_bytes[:col].decode())`.
2. **Collect tokens.** For a query's `TemplateStr` node N, take the `tokenize` tokens that
   start within N's span. Walk them at brace depth 0, counting depth with `OP '{'` and
   `OP '}'`, and collect the `TSTRING_MIDDLE` tokens. Prefix letters come from the
   preceding `TSTRING_START`; `r` means raw. Nested strings inside interpolations sit at
   depth > 0 and are ignored.
3. **Decode each middle token** into characters, each with a (line, col) position.
   Escapes are handled as follows:
   - `\<newline>` is dropped;
   - `\\ \' \" \a \b \f \n \r \t \v`, `\ooo`, `\xhh`, `\uXXXX`, `\UXXXXXXXX` and `\N{…}`
     each produce 1 character;
   - an unknown escape such as `\q` produces 2 characters;
   - `\r\n` becomes one `\n`;
   - in raw strings, every character maps to itself.

   For `{{`/`}}`, the token already holds a single brace, and the second source brace is a
   position gap that is ignored.
4. **Verify.** Split the decoded characters into segments at the interpolations. Each
   segment **MUST** equal the corresponding `ast.Constant.value`, with adjacent Constants
   concatenated. On any mismatch, mark the segment `inexact`: its carets fall back to the
   start of the segment. A wrong caret is never emitted.
5. **Compile.** Call `_template.compile_parts(segment_strings)`, which gives
   `Compiled.segments` and `.placeholders`. Then build `sql_index → source position`.
6. **Map server positions.** The server's `position` is a 1-based **character** index into
   the SQL. Take `p − 1`:
   - inside a placeholder range, it maps to the whole `{expr}` span, braces included;
   - inside an exact segment, it maps to that character's position;
   - inside an inexact segment, it maps to the segment start;
   - `p − 1 == len(sql)` (an error at end of input) maps to the end of the last segment;
   - a space inserted by the adjacency rule maps to the following placeholder.

Required fixtures:

- leading non-ASCII text;
- `\n`, `\x41` and `é`;
- `{{ }}`;
- `rt""`;
- a triple-quoted multi-line string;
- a CRLF file;
- parenthesised multi-line `t"" t""` concatenation;
- an error located after `{x}`;
- an error at end of input.

## Resolving T (`check/resolve.py`)

`T` is the subscript expression, or `None` when there isn't one. It is parsed into the
[Row-type grammar and runtime row plan](api.md#row-type-grammar-and-runtime-row-plan) grammar.

- Leaf names are matched by their **resolved import**: `int`, `decimal.Decimal`,
  `datetime.date`, `typing.Any`/`typing_extensions.Any`, `typing.Optional`/`Union`/
  `Annotated`, and so on. This works whatever the local alias is (`import datetime as dt;
  dt.date`).
- Quoted strings inside T are `invalid-row-type`.

**Aliases.** A name is resolved in the module where it appears. Supported binding forms
are `type X = …`, `X: TypeAlias = …`, and a module-level `X = <pure type expression>` (no
calls). The import table is followed through:

- absolute and relative imports;
- `import m as x; x.Name`;
- `__init__` re-exports;
- `from m import *`, within project modules only, using `__all__` when it is a literal.

Additional rules:

- A name bound more than once at module level is `unresolved-row-type`.
- Generic aliases are `invalid-row-type`.
- At most 16 hops are followed. A cycle is `unresolved-row-type`.
- `TYPE_CHECKING` branches are followed, and the first binding wins.

**Module graph.**

- The roots are the `include` directories. For a `src/` layout, the directory containing
  the file's top-level package is also a root.
- `sys.path` is **never** read, and user code is **never** imported or executed. A test
  uses a module that raises at import, and asserts that `sys.modules` is unchanged.

**Diagnostic location.** The primary span is the `T` expression at the call site. When `T`
came from an alias, a `note: alias defined at path:line:col` is added.

## Rules (all errors)

| Rule | Condition |
|---|---|
| `syntax-error` | The file can't be decoded or parsed. |
| `indirect-query` | A disallowed use of `query`/`query_unchecked` ([Discovery](#discovery-checkdiscoverpy)). |
| `dynamic-query` | The argument is not one inline t-string, or it has a conversion, format spec, or evident nested Template. |
| `unresolved-row-type` | `T` can't be resolved statically. |
| `invalid-row-type` | `T` isn't derivable from the [Row-type grammar and runtime row plan](api.md#row-type-grammar-and-runtime-row-plan) grammar. |
| `sql-error` | The server rejected the SQL at Parse. The diagnostic carries sqlstate, message, detail and hint. Help text is added for 42P18/42P08 ("cast the parameter: `{x}::int`") and for 42501. |
| `param-count-mismatch` | `len(describe.params) ≠` the number of interpolations. It is reported at the first interpolation whose `$n` the server doesn't reference, or at the t-string. Help: "an interpolation inside a string literal or comment is not a parameter". |
| `missing-row-type` | `T` is `None` and the statement returns at least one non-`void` column. |
| `unexpected-row-type` | `T` is not `None` and the statement returns 0 columns, counting a statement whose columns are all `void` as 0. |
| `arity-mismatch` | A tuple's arity differs from the column count, or a scalar `T` doesn't face exactly 1 column. When this fires, no per-element rules run for that call. |
| `unsupported-type` | A column or parameter type isn't in the table, or has `typsend = 0`. |
| `type-mismatch` | The column's Python type ([Type mapping](type-mapping.md#type-mapping-single-source-of-truth)) differs from the element's leaf, ignoring `None`. `bool` and `int` are distinct. Naive versus aware datetimes are not checked. |
| `nullability-mismatch` | The column is nullable but the element lacks `None`, **or** the column is NOT NULL but the element includes `None` (exact). |
| `invalid-override` | An alias ending in `!`/`?` was truncated at 63 bytes ([Nullability overrides](api.md#nullability-overrides-sqlx-convention)). |
| `param-type-mismatch` | An interpolation's evident Python type ([Parameter type inference](#parameter-type-inference-checkparamspy-sqlty-style)) isn't in that parameter's `accepted_params` ([Type mapping](type-mapping.md#type-mapping-single-source-of-truth)). |

Exceptions to exact matching:

- `Any` and `object` match any column type and either nullability; they are the only
  nullability-neutral annotations.
- JSON columns accept any leaf annotation. Nullability rules still apply unless the
  element is `Any` or `object`.
- An array element may be `X` or `X | None`, at every nesting level.
- A `void` column matches only the tuple element `None`. A scalar `T` facing a `void`
  column is `type-mismatch`, with help "use .execute()".

## Parameter type inference (`check/params.py`, sqlty-style)

The type is **evident** only in these cases. In every other case it is unknown, and an
unknown type is **never** reported.

- **Literals:** `int`, `float`, `str`, `bytes`, `bool`, `None`, an inline t-string (giving
  `Template`), and list or tuple literals whose elements are all of one evident type.
  (`list[int]` matches an array parameter element-wise.)
- **Calls of known constructors:**
  - `decimal.Decimal(...)`, `uuid.UUID(...)`;
  - `datetime.date(...)`, `datetime.datetime(...)`, `datetime.time(...)`,
    `datetime.timedelta(...)`;
  - `datetime.datetime.now(...)` gives `datetime`;
  - the builtins `bytearray(...)` and `memoryview(...)`, which stay unknown when the name
    is shadowed or rebound.

  Constructors are matched by their resolved import.
- **Names.** The name must have exactly one binding in its innermost function or module
  scope. That binding must be one of:
  - an annotated function parameter;
  - an `AnnAssign`;
  - an `Assign` whose right-hand side is evident.

  The type is unknown if anything in that scope uses an `AugAssign`, `global`, `nonlocal`,
  `del`, walrus, loop target, `with … as`, unpacking target or `except … as` on the name.
  Comprehension variables have their own scope. Class-body names are invisible to methods.
  Closures follow normal lexical lookup. In an annotation, `X | None` counts as `X`, and a
  union of several non-None types counts as unknown.

## Output

Text format:

```text
error[nullability-mismatch]: column 3 "email" may be NULL but tuple element 3 is `str`
  --> app/users.py:14:43
   |
14 |         user = await query[tuple[int, str, str]](
   |                                            ^^^ element 3
   |
   = note: nullable because users.email has no NOT NULL constraint
   = help: annotate `str | None`, or alias the column as "email!" if it can't be NULL
```

- Carets for element rules point at that **element** when `T` is inline, and at the `T`
  expression, plus an alias note, otherwise.
- `sql-error` carets come from [Source map](#source-map-normative-algorithm-checksourcemappy).
- Nullability notes are rendered from `NullReason`. Examples: "right side of LEFT JOIN
  (Hash Left Join)" and "computed expression; add AS \"col!\"".

JSON format: stdout carries **one JSON array**, which is `[]` when the run is clean. Each
item has these fields:

```text
{ "rule", "message", "file" (project-relative, forward slashes), "line", "col",
  "end_line", "end_col" (1-based code points, end exclusive), "notes": [str],
  "help": str|null, "sqlstate": str|null, "sql": str|null, "column_index": int|null }
```

Exit-2 failures go to stderr as text. The DSN or password is never printed.

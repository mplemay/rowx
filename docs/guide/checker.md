# The checker

```text
rowx check [PATHS...] [--database-url URL] [--config FILE] [--output-format text|json] [--statistics] [--coverage] [--inspect] [--require-checked] [--disallow-unchecked] [-j N] [--scratch] [--no-drift-check] [--error RULE] [--warn RULE] [--ignore RULE] [--exit-zero-on-warning] [--respect-ignore-files | --no-respect-ignore-files] [--force-exclude]
```

The checker never imports or executes your code. It parses your files, finds every `query[T](t"...")` call, resolves `T`
statically, and asks Postgres to prepare and describe the SQL. Then it compares the columns and parameters with your
types. See [the checker rules](../reference/checker.md#rules) for the rules.

| Exit code | Meaning |
|---|---|
| 0 | No failing diagnostics, including when no queries were found or only warnings remain with warning failure disabled. |
| 1 | An error, or a warning when warnings fail the check (the default). |
| 2 | Usage or config error, an unreachable database, a describe failure that is not an SQL error, or an internal error. |

Configuration lives in `pyproject.toml`:

```toml
[tool.rowx]
include = ["src"]                 # default ["."]
exclude = ["**/migrations/**"]
database_url_env = "DATABASE_URL"
statement_timeout = "10s"         # integer + unit: ms|s|min
```

`[tool.rowx.migrate]` is described under [Migrations](migrations.md).

The database URL comes from `--database-url`, else from the environment variable named by `database_url_env`.

That URL is an explicit check target, in the style of SQLx. `database_url_env` defaults to `DATABASE_URL`. The
checker does not infer which executor will run a query. It checks every recognized `query` and `query_unchecked`
construction, including one nothing calls, on the public import roots `rowx` and `rowx_core`. At
runtime the executor passed to the method supplies the compiler, and that compiler emits the SQL for the call. A
later call on another executor compiles again. There is no automatic multi-target routing and no offline query
metadata.

## Checker suppressions and rule levels

Rowx uses its own comments; `type: ignore`, `noqa`, and `@no_type_check` do not suppress SQL checks. A trailing
comment such as `# rowx: ignore[invalid-row-type]` suppresses that rule when its diagnostic starts or ends on that
line, using the last occupied line of an end-exclusive span. A comment in the middle of a multiline span does not
match. Multiple rules can be comma separated. `# rowx: ignore` suppresses ordinary rules on that line. A standalone
comment before the first code token applies to the whole file; a standalone comment after code does not apply to the
next line. A reason can follow the directive as `# rowx: ignore[sql-error] # reason`. Unrecognized or malformed
comments and unused entries produce `invalid-ignore-comment` and `unused-ignore-comment` warnings. Suppression changes
reporting only: source discovery, SQL description, and coverage still run. Operational failures remain errors.
If tokenization fails, comments in that file are not applied and no unused-ignore warnings are generated.

Configure levels in `pyproject.toml`:

```toml
[tool.rowx.rules]
all = "error"
unused-ignore-comment = "warn"
```

Levels are `error`, `warn`, and `ignore`. Specific settings override `all`; repeatable `--error RULE`, `--warn RULE`,
and `--ignore RULE` options apply in command-line order and also accept `all`. Warnings fail the check by default;
`--exit-zero-on-warning` or `[tool.rowx.terminal] error-on-warning = false` changes that. JSON diagnostics include
`severity`. Direct checker results and `check-compat` retain raw evidence regardless of reporting settings;
compatibility snapshot discovery bypasses ignore files while retaining the snapshot's rowx exclusions. Baselines,
automatic ignore insertion, and generic Python type-checker suppression compatibility are outside this feature.

## Source selection

`[tool.rowx] include` and `exclude` patterns are anchored to the project root. Use `**/generated` for a basename at any
depth (previously `generated` did this); `generated` now refers to the root directory. Excludes accept ordered `!`
reinclusions. For import resolution, each include contributes its literal directory prefix before the first glob
component as a source root. Rowx reads nested `.gitignore` and `.ignore` files even outside Git; tracked files matching
those rules are excluded, and untracked files are included unless ignored. Set `respect-ignore-files = false` or pass
`--no-respect-ignore-files` to disable these files. Explicit paths bypass filtering for the selected path itself;
descendants of explicit directories are still filtered. Use `force-exclude = true` or `--force-exclude` to filter
explicit paths too. The `.git` directory is always excluded.

## Hand-built templates

`Template(...)` with a non-literal string is raw SQL: the string becomes part of the statement text and is not a
bound parameter. The `template-construction` rule keeps that path out of your code. It runs on **every file under the
include roots**, not only on files with queries, because a helper can build a template in one file and pass it to
`query_unchecked` in another. It reports:

- a `Template(...)` call with any argument that is not a `str` literal, an inline t-string or an `Interpolation(...)`
  call, including a starred argument (`Template(*parts)`) and any keyword argument;
- an `Interpolation(...)` call whose `conversion` or `format_spec` is not a literal, or that takes a starred or `**`
  argument;
- an indirect use of the class that hides the call, such as `T = Template`, passing `Template` as a value,
  `Template.__new__`, `from string.templatelib import *` or `type(t"...")("...")`;
- `t"..." + x` where `x` is neither an inline t-string nor evidently a `Template`;
- a format spec that is not a literal in any t-string (`t"{x:{spec}}"`). The t-string that is the direct argument of
  `query_unchecked` reports it as `dynamic-query` instead, so one interpolation is never reported twice.

Aliased imports (`from string.templatelib import Template as T`) are followed, and so are the `Template` and
`Interpolation` that private modules of this package re-export (`from rowx_core._template import Template`). A subscript
is type-level only when it subscripts a name (`list[Template]`), so `[Template][0](x)` is reported. A class pattern
(`case Template(strings=s)`) and a name assigned once from an evident `Template` are accepted. Build dynamic SQL by
nesting t-string literals instead (`t"WHERE {cond}"`), which keeps every value a parameter.

The rule is **best-effort**. It reads source only, so `getattr`, `eval`, dynamic imports, generated code and code
outside the include roots can still build a template. So can reflection: `type(x)(...)` on a value that is not evidently
a `Template`, `__reduce__` and pickle, `__annotations__`, `typing.get_type_hints` and `inspect.signature`, `globals()`,
`locals()` and `vars()` (read, or assigned through), `sys.modules[__name__]`, `setattr` on a module and
`operator.attrgetter`.

Only selected `*.py` files are checked; `.pyi` stubs and extensionless scripts are not. A file nested too deeply to
analyse (thousands of chained operators or attributes) is reported as a `syntax-error` without hiding other files.

## Query check coverage

Use `rowx check --inspect PATH` to show each successfully described query's columns, types, nullability reasons,
and parameter comparison evidence. It does not execute queries or import source modules. Inspection appears on stderr
when `--output-format json` is selected, leaving the diagnostics JSON unchanged.

Use `rowx check --coverage` to list recognized direct query sites and their description and comparison evidence. The
report goes to stderr with `--output-format json`; JSON diagnostics remain on stdout. A described parameter can have a
known PostgreSQL type while its Python type is unknown. A lack of a mismatch diagnostic does not prove compatibility
when a type is unknown or unsupported, or the parameters cannot be aligned. Calls rejected before description are
listed as not described. Indirect or ambiguous uses retain their existing diagnostics.

Row contracts are reported as compared, partially compared, or not compared. Partial comparison means that some row
checks were skipped, for example because of a structural mismatch, an unsupported column type, or unavailable field
names. An unresolved row contract is not compared.

For CI, `--require-checked` requires at least one successfully described query, and `--disallow-unchecked` rejects
recognized `query_unchecked` sites. Both are opt-in. Unchecked-only files need no database connection.

For a finite choice of SQL syntax, write each alternative as a separate ordinary query so both are checked, even if
one branch is not exercised at runtime:

```python
from rowx import query


def ordered(descending: bool = False):
    if descending:
        return query[int](t'SELECT 1 AS "value!" ORDER BY 1 DESC')
    return query[int](t'SELECT 1 AS "value!" ORDER BY 1 ASC')
```

## Safety

- The checker performs Parse/Describe, catalog lookups and `EXPLAIN` only, never `ANALYZE`. It does not execute your
  statements.
- Postgres may still **evaluate user-defined IMMUTABLE and STABLE functions while planning**, and it takes locks on
  referenced relations.
- Run it as a dedicated non-superuser role against a CI or disposable database.

With `--scratch` the statement is amended:

- The checker still never imports or executes your Python, and never executes your checked queries.
- It does execute the **migration files**, and only in the database it created for the run, as the connecting role.
  Migration files are trusted code. The role needs `CREATEDB`.

`rowx check --scratch` needs `[tool.rowx.migrate]`. It treats the database URL as a server URL, creates
`rowx_check_<hex>` (`TEMPLATE template0`), runs the migrations in it, checks against it, and drops it `WITH (FORCE)` on
every exit path. The drift gate doesn't apply. A missing `CREATEDB` privilege and a failing migration both exit 2.

The scratch database is tagged with `COMMENT ON DATABASE 'rowx:<pid>@<host>:<unix-ts>'`. At startup the checker drops
any `rowx_check_*` database whose tag is over an hour old and which has no sessions, ignoring errors, so a SIGKILLed or
cancelled CI run doesn't leak its database. Administrative connections use the URL's own database, or `postgres`
(`template1` when the target is `postgres`), and fall back from `postgres` to `template1` where `postgres` is missing or
refused.

The checker's session is read-only by default (`default_transaction_read_only = on`) and sets `statement_timeout`,
`lock_timeout` and `idle_in_transaction_session_timeout`.

## Checking rollout compatibility

Use a disposable PostgreSQL server and trusted migration files to check both rollout orders:

```sh
rowx check-compat --against BASE --head HEAD --database-url "$ROWX_DATABASE_URL"
```

The command independently replays each revision's migrations in scratch databases. Those databases share the
server's roles and other cluster resources. It checks each revision on its own schema before checking baseline
source on the head schema (migrate first) and head source on the baseline schema (deploy first). It reports
recognized and skipped sites for each revision. Exit status 0 means the checked subset is compatible, 1 means a
cross-schema incompatibility was found, and 2 means the check was incomplete.

The checker never executes checked queries. PostgreSQL 18.3 accepts Parse/Describe and EXPLAIN for an INSERT that
omits a newly added NOT NULL column without a default, although executing that INSERT fails. Runtime constraints,
production data and permissions are outside this check's coverage.

For example, `INSERT INTO t (id) VALUES (1)` can still be described after
`ALTER TABLE t ADD COLUMN required text NOT NULL`, but fails when executed.

For example, run `docker run --rm -e POSTGRES_PASSWORD=example -p 127.0.0.1:5432:5432 postgres:18` and set
`ROWX_DATABASE_URL=postgresql://postgres:example@127.0.0.1:5432/postgres`. Pass the URL through `--database-url` or
`ROWX_DATABASE_URL`; historical configuration does not select the server. The role needs `CREATEDB`. The workflow
rejects symbolic links and Git submodules in snapshots.

With no server at all, `--database-url embedded://` builds both schemas in memory. See
[Embedded Postgres](embedded.md#check).

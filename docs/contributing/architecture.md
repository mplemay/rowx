# Architecture

The reference pages are the user contract. This page is the current tree: where the code lives, how a backend is
linked, and which checks run without a database.

## Distributions

| Distribution | Import root | Runtime dependencies |
|---|---|---|
| `rowx-core` | `rowx_core` | none |
| `rowx-postgres` | `rowx_postgres` | matching `rowx-core` only |
| `rowx` | `rowx` | matching `rowx-core`; the `postgres` extra adds matching `rowx-postgres` |

`from rowx import query` and `from rowx_core import query` are the same object. `import rowx` loads no backend.
Connections are `from rowx_postgres import Pool, connect`. The `postgres` extra is what installs that package. The
backend does not depend on the CLI.

Python packages:

| Path | Role |
|---|---|
| `packages/rowx-core/src/rowx_core/` | Shared query, template capture, executor protocols, errors, migration contracts. |
| `packages/rowx-postgres/src/rowx_postgres/` | PostgreSQL connection, pool, compiler, value types, `Migrator`, bound services. |
| `src/rowx/` | CLI, checker, pytest plugin, and the core re-exports. |

Rust crates:

| Path | Role |
|---|---|
| `crates/rowx-core` | No PyO3. Claim/settle (`state.rs`), cancel plumbing (`cancel.rs`), row shape (`row_shape.rs`), pool, migration plan. |
| `crates/rowx-postgres` | No PyO3. DSN, TLS, connection, execution, statement cache, describe, explain, wire codecs. Depends only on `rowx-core` among the workspace crates. |
| `crates/rowx-py-core` | Shared PyO3 support: the tokio runtime (`runtime.rs`) and the exception-class registry (`errors.rs`). |
| `packages/rowx-postgres` | The cdylib. Cargo package name `rowx-postgres-py`, library name `_core`. This is the binding crate, not a separate distribution. |

Workspace members are those four paths. `pyo3` and `pyo3-async-runtimes` are workspace dependencies and their minor
versions move together. `uv sync` installs the workspace. `ty` checks each package in place, with no merged
type-check tree and no cross-package symlink. Build the extension with
`uv run maturin develop -m packages/rowx-postgres/Cargo.toml`. Do not `cargo build` the cdylib on its own.

## Native module and runtimes

The Python module is `rowx_postgres._core` (`module-name` in `packages/rowx-postgres/pyproject.toml`).
`packages/rowx-postgres/src/lib.rs` registers it and calls `rowx_py_core::runtime::init()` from the module
initialiser. That builds one multi-thread tokio runtime for the extension, named `rowx-io`, and registers it with
`pyo3-async-runtimes`. The runtime static lives in the extension, so each backend extension links
`rowx-py-core` statically and owns its runtime. Only the PostgreSQL extension exists today.

The GIL is released around IO. IO runs on that runtime.

## Acyclic dependencies

`rowx-postgres` does not import `rowx`. The checker in `rowx` selects a backend from the database URL and receives
the backend's checker, admin and migration services as arguments. `Backend` in
`packages/rowx-core/src/rowx_core/backend.py` has `checker`, `admin`, `migrations`, and `testing` factories. A
missing one raises `UnsupportedCapabilityError`. A checker-only backend need not implement administration or
testing. There is no `ContextVar`, no default backend, and no cast through `Any` to hide a backend type. A
capability the process did not install is an error from `backend_for`, not a silent PostgreSQL fallback. Keyword
and `postgres`/`postgresql` URLs ask for `rowx_postgres`. Error redaction is string processing over the URL the
command already holds. It does not import a provider.

## Validation timing

Two steps, on purpose.

- Capturing a t-string is backend-independent and runs in the `query` constructor (`rowx_core`). It rejects a
  non-template, a NUL in static text, a conversion and an unsupported format spec. Nested templates stay a DAG.
  Expansion limits belong to the dialect.
- Emitting SQL is dialect-dependent and runs when a method is called, using the compiler on the executor passed in.
  The PostgreSQL compiler (`rowx_postgres._compiler`) emits `$n`, inserts adjacency spaces, and rejects more than
  65535 parameters. A later call on another executor compiles again. The checker does not infer which executor that
  will be. It checks the construction site, including a query nothing calls, and it checks both import roots.
- The checker's source map calls that same compiler's `compile_parts` and uses the padding it returns. Runtime SQL
  and checker spans cannot drift apart by inventing a second spacing rule.

Describe against Postgres is dialect-dependent and needs a database URL. It is skipped for `query_unchecked` SQL.
The row type is still resolved. Migration filenames, checksums and the plan are backend-independent and run in
`rowx_core` / `crates/rowx-core/src/migrate/` with no connection. Applying a migration is dialect-dependent and goes
through `rowx_postgres`.

## Drift and migrate add

The drift gate reads history through `migrator.open(readonly=True)`. That connection is the checker's read-only
session. The read takes no advisory lock and creates nothing.

`rowx migrate add` calls the shared source discovery in `rowx_core`. It does not call `backend_for`. An existing
migration directory is not a reason to load a backend. `run`, `revert`, `skip` and `info` do load one.

`rowx database reset` and `setup` construct the `Migrator`, which validates the files, before they create, drop or
change a database. A bad directory fails before the destructive step. `drop` alone does not read the files. Only
opening a connection is retried. The migration command runs once.

## Deferred

These items are not specified in the reference and are tracked by the issues below.

- [#296](https://github.com/mplemay/rowx/issues/296) checker suppressions and rule configuration
- [#297](https://github.com/mplemay/rowx/issues/297) remaining named row types
- [#298](https://github.com/mplemay/rowx/issues/298) opt-in query pipelining
- [#299](https://github.com/mplemay/rowx/issues/299) offline query cache
- [#300](https://github.com/mplemay/rowx/issues/300) multi-statement execution and command tags
- [#301](https://github.com/mplemay/rowx/issues/301) checker `--fix`
- [#302](https://github.com/mplemay/rowx/issues/302) column type overrides and custom codecs
- [#303](https://github.com/mplemay/rowx/issues/303) parser-based nullability narrowing
- [#304](https://github.com/mplemay/rowx/issues/304) listen/notify, copy, client certificates, libpq env and
  free-threading
- [#305](https://github.com/mplemay/rowx/issues/305) additional database backends
- [#306](https://github.com/mplemay/rowx/issues/306) package-resource migrations
- [#307](https://github.com/mplemay/rowx/issues/307) per-test SQL fixtures for `rowx.testing`

Embedded Postgres is [#194](https://github.com/mplemay/rowx/issues/194).

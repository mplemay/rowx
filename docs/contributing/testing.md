# Testing

Each package owns `<import_root>/__tests__/`, with `unit/` and `integration/` beside the code they test. Core and
CLI tests use fake executors or services and need no database. Database tests belong to `rowx_postgres`.

| Tree | What it holds |
|---|---|
| `packages/rowx-core/src/rowx_core/__tests__/unit/` | Row plan, errors, migration source logic. No database. |
| `packages/rowx-core/src/rowx_core/__tests__/support/row_types.py` | The shared row-type table the core row plan executes. |
| `src/rowx/__tests__/unit/` | CLI, checker discovery and config, with fakes. No database. |
| `packages/rowx-postgres/src/rowx_postgres/__tests__/unit/` | Compilers, codecs and checker pieces that do not need a server. |
| `packages/rowx-postgres/src/rowx_postgres/__tests__/integration/` | Queries, describe, migrations, the checker against Postgres. |
| `packages/rowx-postgres/src/rowx_postgres/__tests__/conformance/` | The row-type grammar against the runtime and the checker, and the pyright/mypy reveal test. |
| `packages/rowx-postgres/src/rowx_postgres/__tests__/support/schema.sql` | The per-test schema. |

A test file is `test_<module>.py` for the module it covers.

## Database

`scripts/pg.sh` starts `postgres:18`. Tests read `ROWX_TEST_DSN`. When it is unset, tests marked `db` skip. When
`ROWX_REQUIRE_DB=1`, as CI sets on Linux, they fail instead. Do not `sleep` to synchronise. Timing tests poll
`pg_stat_activity` and are marked `timing`.

Integration tests build a schema `t_<hex>` from `support/schema.sql` and set `search_path` through the DSN. They do
not use `public`. The nullability evidence cases live in
`integration/describe/test_nullability_regression.py`, one case per row of the
[evidence table](../reference/nullability.md#evidence), in that order. Known-limit cases live in
`integration/describe/test_known_limits.py`. Checker fixtures live under `integration/check/cases/`. Snippet tests
execute fences tagged `python test` in `integration/docs/test_snippets.py`. The snippet glob includes the
[use-rowx skill](https://github.com/mplemay/rowx/blob/main/.agents/skills/use-rowx/SKILL.md). Ordinary `python`
fences are not executed.

Rust tests are `cargo test --workspace` (`just test-rust`). Database tests in `crates/rowx-postgres/tests/` use the
same DSN. `crates/rowx-core` has no database.

## Commands

| Command | What it runs |
|---|---|
| `just check` | `uv run prek run --all-files`: rumdl, ruff, `ty check` on `src`, `packages/rowx-core/src` and `packages/rowx-postgres/src`, `cargo fmt`, `cargo clippy`. |
| `just test-py` | `uv run pytest`. |
| `just test-rust` | `cargo test --workspace`. |
| `just docs-build` | `zensical build --clean --strict`. |

`ty` is the type-check gate. pyright and mypy stay in the dev group because
`conformance/test_typecheckers.py` runs both against the public query annotations. They are not part of `just check`.
Linux CI runs `just check` and the database tests against `postgres:18`. The macOS job runs `just check` and does
not start a database.

## What the suites pin

- **Row types.** `support/row_types.py` in core and `conformance/row_types.py` in `rowx_postgres` are the grammar.
  A form the runtime accepts and the checker rejects is recorded as such. The two tables are not one file. Core
  rows do not need a database. Conformance rows that describe SQL do.
- **Types.** `crates/rowx-postgres/src/types.rs` is the only OID table. A new leaf joins the generated
  [type table](../types.md), the codecs, and the conformance rows. Wire codecs are unit-tested as pure functions.
- **Nullability.** The evidence table is an integration test, in row order. The plan walker has Rust tests on
  captured plans. A NOT NULL verdict that a NULL contradicts fails. Over-promotion is reported and is not a failure
  by itself. The limits are [Known limits](../known-limits.md).
- **Checker.** Discovery, the source map, resolution and comparison have unit tests. `integration/check/` runs the
  CLI against Postgres, including scratch databases, drift, and migration commands. `query_unchecked` cases assert
  that SQL is not described and that a bad `T` is still reported.
- **Migrations.** Filename grammar and the plan are tested without a database. Apply, revert, the advisory lock and
  the history table are `rowx_postgres` integration tests. `migrate add` is tested with no backend installed.
- **Distributions.** `src/rowx/__tests__/unit/test_validate_distributions.py` checks the three packages: `rowx-core`
  has no runtime dependencies, `rowx-postgres` depends only on `rowx-core`, and `rowx` does not depend on
  `rowx-postgres` unless the extra is selected.

## Test plugin

`rowx.testing` (`src/rowx/testing.py`) is opt-in. Nothing registers it as a pytest plugin entry point. A project
sets `pytest_plugins = ["rowx.testing"]` and depends on `rowx[pytest]`, which adds pytest and does not add a
database backend. The plugin reads `[tool.rowx.migrate]` and the database URL. It builds a template database and
gives each test a clone. Without a URL the tests skip, or fail when the project requires a database. The plugin
tests under `src/rowx/__tests__/unit/test_testing.py` use fakes and do not connect.

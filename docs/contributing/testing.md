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
| `packages/rowx-postgres/src/rowx_postgres/__tests__/support/engine.py` | The embedded engine's recorded divergences ([Embedded lane](#embedded-lane)). |
| `packages/rowx-embedded/tests/` | The embedded companion's own tests, run by `.venv/bin/python -m pytest packages/rowx-embedded/tests` and `cargo test --manifest-path packages/rowx-embedded/Cargo.toml --workspace`. |

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
execute fences tagged `python test` in `integration/docs/test_snippets.py`. Ordinary `python` fences are not
executed.

Rust tests are `cargo test --workspace` (`just test-rust`). Database tests in `crates/rowx-postgres/tests/` use the
same DSN. `crates/rowx-core` has no database.

## Commands

| Command | What it runs |
|---|---|
| `just check` | `uv run prek run --all-files`: rumdl, ruff, `ty check` on `src`, `packages/rowx-core/src` and `packages/rowx-postgres/src`, `cargo fmt`, `cargo clippy`. |
| `just test-py` | `uv run pytest`. |
| `just test-rust` | `cargo test --workspace`. |
| `just docs-build` | `zensical build --clean --strict`. |
| `just check-embedded` | Clippy for the embedded companion, a separate Cargo workspace: `uv run prek run cargo-clippy-embedded --hook-stage manual`. |
| `just embedded-dev` | `uv sync --extra embedded`: builds the engine into the venv. |
| `just test-embedded` | `ROWX_REQUIRE_EMBEDDED=1 uv run --extra embedded pytest -m embedded`. |

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
- **Distributions.** `src/rowx/__tests__/unit/test_validate_distributions.py` checks the three lockstep packages:
  `rowx-core` has no runtime dependencies, `rowx-postgres` depends only on `rowx-core`, and `rowx` does not depend on
  `rowx-postgres` unless the extra is selected. Its `embedded` extra must add exactly `rowx-postgres` and the platform
  gated `rowx-embedded>=18.4,<19`. The embedded wheels have their own checks, `scripts/validate-embedded-wheels.py`
  and `scripts/validate-embedded-release.sh`.

## Test plugin

`rowx.testing` (`src/rowx/testing.py`) is opt-in. Nothing registers it as a pytest plugin entry point. A project
sets `pytest_plugins = ["rowx.testing"]` and depends on `rowx[pytest]`, which adds pytest and does not add a
database backend. The plugin reads `[tool.rowx.migrate]` and the database URL. It builds a template database and
gives each test a clone. Without a URL the tests skip, or fail when the project requires a database. The plugin
tests under `src/rowx/__tests__/unit/test_testing.py` use fakes and do not connect.

## Embedded lane

| Marker | Meaning |
|---|---|
| `db` | Needs a live Postgres (`ROWX_TEST_DSN`). |
| `server(needs=...)` | Needs a real server: `sessions`, `cancel`, `tls`, `create-database`, `pg-stat-activity`, `timing`, `auth` or `tcp`. `timing` implies `server`. |
| `server(divergence=...)` | Hits an engine divergence: `inet-cidr`, `xml`, `encoding`, `locale` or `backend-pid`. |
| `embedded` | Needs the companion; fails instead of skipping when `ROWX_REQUIRE_EMBEDDED=1`. |

With `ROWX_TEST_DSN=embedded://` the suite starts one instance per process (per xdist worker) and points the tests at
it; `server` tests skip, a test that skips for any other reason fails (so a lost precondition cannot shrink the lane),
and a test that leaves a session open fails the next tests with one line naming it. The lane is
`pytest -m "(db and not server) or embedded"`. Docker stays the full-capability lane, so `ROWX_REQUIRE_DB=1` runs with a
server run everything.

The divergences are recorded in `packages/rowx-postgres/src/rowx_postgres/__tests__/support/engine.py`. Removing a mark
is the fix for its cause:

| Divergence | What differs |
|---|---|
| `inet-cidr` | Binary `inet` and `cidr` values carry WASI address-family bytes. |
| `xml` | The engine has no XML support (`0A000`). |
| `encoding` | No conversion libraries: `SET client_encoding = 'SJIS'` fails. |
| `locale` | No locale data: `lc_monetary` stays `C`. |
| `backend-pid` | Every session reports the same `pg_backend_pid()`. |

Not recorded as a mark, because the tests that touch it are engine tests: closing a directory instance stops the
backend as a process exit does, with no shutdown checkpoint, so the next open runs crash recovery. The host runs a
`CHECKPOINT` just before a normal close (never on an abandon), so a sequence does not skip the 32 values it pre-logs.
`UNLOGGED` relations are still reset by every reopen, which a server does only after a crash.

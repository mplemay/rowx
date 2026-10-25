# Testing strategy

- **Provisioning.**
  - `scripts/pg.sh up [--ssl]`, `down` and `dsn` run `postgres:18` on a random localhost
    port, with `-c max_connections=200`, and print the DSN.
  - `scripts/gen-certs.sh` generates the TLS material: a CA, a server certificate for
    localhost and 127.0.0.1, and a wrong-host certificate.
  - Tests read `ROWX_TEST_DSN`. When it is unset, DB tests **skip** locally, and they
    **fail** when `ROWX_REQUIRE_DB=1`, which CI sets.
  - Docker Desktop, OrbStack or colima is needed locally on macOS.
- **Isolation.** Each test gets a schema `t_<hex>`, created from `tests/schema.sql` and
  selected with `search_path`, set through the DSN `options=-c search_path=…`. Rollback-only
  isolation is not enough, because the cancel, TLS and busy tests need real sessions.
- **Seed schema** (`tests/schema.sql`) includes:
  - `users`, `posts` and `orders` ([Evidence: why not sqlx's describe](nullability.md#evidence-why-not-sqlxs-describe));
  - an enum, a domain and a domain array;
  - the `citext` extension;
  - a table with every [Type mapping](type-mapping.md#type-mapping-single-source-of-truth) type;
  - a `NOT VALID` NOT NULL column;
  - a foreign table (via `file_fdw`) or a view.

  Seed data (`tests/seed_data.sql`) holds NULL-heavy rows.
- **Markers.** `db` marks tests that need Postgres, and `timing` marks the cancel, busy
  and timeout tests. Timing tests synchronise with `pg_stat_activity` polling, not sleeps,
  and use generous CI bounds.
- **Rust.**
  - Pure unit tests: the walker on captured plan fixtures, wire codecs, the LRU, the state
    machine and DSN parsing.
  - Integration tests (`crates/rowx-pg/tests/*.rs`) use `tests/common/mod.rs` for the DSN
    and a fresh schema.
- **Plan fixtures.** `scripts/capture-explain.sh` writes
  `crates/rowx-pg/tests/fixtures/plans/*.{sql,json}`. `--check` regenerates them and diffs
  the result.
- **Goldens.** There is one file per type family in `tests/golden/`, holding round-trip
  values, edge cases and expected errors. A meta-test asserts that every `type_table()` row
  has decode and encode goldens, or an explicit exemption.
- **Conformance.** `tests/conformance/row_types.py` runs the shared
  [Row-type grammar and runtime row plan](api.md#row-type-grammar-and-runtime-row-plan) table through `_rowplan` and the
  checker's resolver.
- **Checker cases.**
  - `tests/check/cases/<rule>.py` files carry inline markers: `# error: [rule]` or
    `# error: [rule1, rule2]` on the line where the diagnostic starts.
  - Caret goldens are `tests/check/expected/*.txt` (text) and `*.json`, compared byte for
    byte.
  - A meta-test asserts that every rule in [Rules](checker.md#rules-all-errors) has at least one case.
- **Fidelity.** This runs **only against the test suite's own seeded database**, and is
  the one place rowx tests execute checked queries. Every checked query in the fixtures is
  executed in a rolled-back transaction over the NULL-heavy seed data. The test asserts
  that no NULL appears where `describe` said NOT NULL.
- **Typing.** `tests/typing/cases.py` holds `# reveal: <type>` expectations, plus negative
  cases (`query("…")` and `query(f"…")` must error). Every query method, and `async for`
  over `fetch`, has a case. It runs `pyright --outputjson` and
  `mypy`, with versions pinned and Python version 3.14.
- **CI.**
  - Linux runs everything, with a `postgres:18` service and `ROWX_REQUIRE_DB=1`.
  - macOS arm64 runs the build, `cargo test --workspace --lib` and `pytest -m "not db"`.
  - Lint gates: `cargo fmt --check`, `cargo clippy --workspace --all-targets -D warnings`,
    `ruff check`, `ruff format --check`, `pyright` (strict for `src/rowx`) and `mypy`.
  - The benchmark is a separate, non-blocking job.

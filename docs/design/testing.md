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
  - Integration tests (`crates/rowx-postgres/tests/*.rs`) use `tests/common/mod.rs` for the DSN
    and a fresh schema.
- **Plan fixtures.** `scripts/capture-explain.sh` writes
  `crates/rowx-postgres/tests/fixtures/plans/*.{sql,json}`. `--check` regenerates them and diffs
  the result.
- **Goldens.** There is one file per type family in `tests/golden/`, holding round-trip
  values, edge cases and expected errors. A meta-test asserts that every `type_table()` row
  has decode and encode goldens, or an explicit exemption.
- **Conformance.** `tests/conformance/row_types.py` runs the shared
  [Row-type grammar and runtime row plan](api.md#row-type-grammar-and-runtime-row-plan) table through `_rowplan` and the
  checker's resolver.
- **Oracles.** Each compares rowx with something independent, and each is generated from `type_table()`, so a new
  table row joins all of them. A meta-test fails when a row is missing from one.
  - **Agreement matrix** (#94): types × row-type leaves, nullability and parameters. A query the checker accepts must
    decode to the annotated leaf, and a parameter it accepts must encode.
  - **`*_send` comparison** (#95): the bytes rowx writes equal the server's `*_send` output, with `proptest` properties
    and `cargo fuzz` decoders in a nightly job outside the workspace lints.
  - **psycopg differential** (#108): the same values decode and encode identically in psycopg, including edge cases and
    session settings for text-encoded parameters, and `money` minor units under a changed `lc_monetary`.
  - **Nullability fuzzer** (#109): a NOT NULL verdict that a NULL contradicts fails CI. Over-promotion is reported, not
    gated.
  - A new type family also adds its goldens, grammar conformance rows (`Range[T]`, the network unions, `hstore`) and
    an extension-owner case (a same-named type in another schema is `unsupported-type`).
- **Nullability precision cases** (#105 and #106 must add these; each also runs under the #109 soundness fuzzer):
  - Scan boundary (plan fixtures plus `describe` cases): a unique plain name match (not promoted), a column from the
    null-extended side (promoted), swapped aliases (`SELECT p.title AS name, u.name AS title`, promoted), a computed
    output, duplicate source names, a planner NULL constant, a `MATERIALIZED` CTE referenced twice, and a CTE over a
    subquery.
  - Views: a plain view over NOT NULL columns, a nested view, a view over an outer join, a view with a dropped column
    (`attnum` gap), a view with an extra `INSTEAD` rule, an unreadable `security_invoker` view, a `search_path` trap (a
    same-named table in another schema), a depth-limit chain, and a crafted definition that must not reach `EXPLAIN` as
    a second statement.
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
- **Pool and transactions** (DB tests; timing tests poll `pg_stat_activity`). Acceptance:
  - Acquire is FIFO for waiters and LIFO for idle connections. Waiting past `acquire_timeout` raises `PoolTimeoutError`.
    A closed pool raises `InterfaceError`, and a bad constructor argument raises `ValueError`.
  - A cancelled waiter never runs its query, and its permit returns to the pool. Capacity is never leaked, including
    when a hook raises.
  - A stream iterated after its lease is revoked raises `InterfaceError`. Releasing a Busy connection cancels and
    settles it.
  - Release resets session state (`SET`, advisory locks, `LISTEN`) and keeps the statement cache. It issues `ROLLBACK`
    from idle, in-transaction and failed-transaction states, with no 25P01 warning when the flag is unset.
  - Retirement by `max_lifetime`, `max_queries`, `idle_timeout` and `expire_connections()`. `min_size` is maintained.
    `close(timeout)` and `terminate()` behave as specified, and use after fork raises `InterfaceError`.
  - The schema epoch drops stale statement caches. `ConnectOptions.from_dsn` round-trips the `schema` fixture DSN
    (`options=-c search_path=…`), and `server_settings` survive `RESET ALL`.
  - Transactions: commit, rollback, savepoints, a cancel mid-statement, a second cancel during `__aexit__`, the failed
    flag, a failed `COMMIT`, and `pool.transaction()` returning a clean connection.
  - A 300-iteration cancel stress test over a pool: **zero** follow-ups may fail.
  - Non-gating benchmark: `query.one(pool)` at 64 tasks against asyncpg `pool.fetchrow`.
- **Migrations** (DB tests, each in a fresh schema or database). Acceptance:
  - *Source:* every filename-grammar case (simple, `.up`/`.down`, mixed, a subdirectory and a non-`.sql` file ignored),
    and each resolution error (a malformed name, a duplicate version, an unpaired file, a non-UTF-8 file) naming the
    file. `-- no-transaction` is recognised only as the exact first line.
  - *Compatibility:* checksums equal sqlx's SHA-384 values for fixture files, and sqlx-format history rows and checksums
    run through rowx with `table = "_sqlx_migrations"`. The lock key equals sqlx's for the same database name.
  - *History table:* the default and a schema-qualified `table` are created and quoted, an invalid `table` raises
    `ValueError`, and `create_schemas` runs before the table is created.
  - *Plan (pure `cargo` tests, plus the same cases through Python):* validation order (dirty, missing, modified),
    `ignore_missing`, out-of-order with and without `allow_out_of_order`, `run(target)` and `undo(target)` including
    `0`, a missing `.down.sql` raising before anything is reverted, `skip`, and `info` with a missing table.
  - *Apply:* a transactional file that fails leaves nothing behind. A failed `-- no-transaction` file leaves a dirty row
    and the next run raises `DirtyMigrationError`. `execution_time` is updated after the commit. A `Transaction`
    argument raises `InterfaceError`. `CREATE INDEX CONCURRENTLY` works.
  - *Locking:* two migrators on one database serialise (synchronised with `pg_stat_activity` polling), the lock is
    released after the run and after a failure, and a pooled connection comes back with no advisory lock.
  - *Cancellation:* cancelling `run` leaves a transactional migration unrecorded and a non-transactional one dirty, and
    the connection or pool stays usable. Running through a `Pool` bumps the schema epoch.
  - *CLI:* `migrate add` inference (sequential, timestamp, reversible, simple, with flags and config overrides),
    `run`, `revert` and `info` output and exit codes (0 and 2), `--dry-run` changes nothing, and `database create`,
    `drop`, `reset` and `setup`, including the confirmation prompt, `-y`, `--force` and the `template1` rule. The
    DSN password never appears in output.
  - *Checker:* the drift gate exits 2 for each of pending, modified, missing and dirty and is skipped by
    `--no-drift-check`. `--scratch` creates and drops `rowx_check_<hex>` on success, on a diagnostic, on an error and
    on Ctrl-C, and fails clearly without `CREATEDB`.
- **Test plugin** (`rowx.testing`).
  - *Opt-in:* with the plugin not enabled, a pytest session sees no rowx fixtures, and `import rowx` doesn't import
    pytest.
  - *Isolation:* two tests that write the same rows don't see each other's data, the per-test database is dropped
    afterwards (also on failure), and the template is rebuilt only when a migration changes.
  - *Fixtures:* `rowx_database_url`, `rowx_connection` and `rowx_pool` work under pytest-asyncio and under anyio.
  - *Concurrency:* parallel template builds under xdist are serialised, and stale `rowx_tpl_*` databases are removed
    unless `--rowx-keep-templates` is set.
- **Module split.** `import rowx` loads neither `rowx._core` nor `rowx.postgres`. `rowx._core.postgres` is importable
  and registered in `sys.modules`, its classes report `module == "rowx._core.postgres"`, and `rowx.connect`,
  `rowx.Connection` and `rowx.ping` no longer exist. `Connection`, `Pool` and `Transaction` satisfy `Executor`.
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

## Test plugin (`rowx.testing`)

The plugin gives each test a freshly migrated database. It is opt-in: `pytest_plugins = ["rowx.testing"]` or
`-p rowx.testing`. Isolation is a **template database plus a clone per test**, unlike the schema-per-test `schema`
fixture above, because migrations may create schemas, extensions and roles-level objects that a `search_path` can't
contain.

- A session-scoped template database, `rowx_tpl_<hash>`, is built once from the migrations. `<hash>` is taken over the
  `(version, checksum)` list, so a template is reused across runs until a migration changes. Concurrent builds (xdist)
  are serialised with an advisory lock.
- Each test that uses a fixture gets `CREATE DATABASE rowx_test_<hex> TEMPLATE rowx_tpl_<hash>`, which is dropped
  `WITH (FORCE)` at teardown. The template is closed to connections before it is cloned.
- The fixtures are `rowx_database_url` (a `str`), `rowx_connection` (a `Connection`) and `rowx_pool` (a `Pool`), all
  function-scoped. They are synchronous fixtures that drive setup with `asyncio.run`, which works because rowx
  connections and pools aren't bound to an event loop. So the plugin needs no async pytest plugin, and works with
  pytest-asyncio or anyio tests alike.
- **Config.** The server URL comes from the `rowx_database_url` ini option, else the `database_url_env` variable, and
  the migrations from `[tool.rowx.migrate]`. `--rowx-keep-templates` keeps the template; by default, stale
  `rowx_tpl_*` templates are removed when a new one is built.
- There is no `pytest11` entry point, so installing rowx never changes a pytest session. pytest is not a runtime
  dependency.

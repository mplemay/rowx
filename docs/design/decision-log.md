# Decision log

## Design interview (2026-10-23)

| # | Topic | Decision |
|---|---|---|
| 1 | Backends | PostgreSQL only; 18 only |
| 2 | Runtime | An owned driver on a Rust library, which is tokio-postgres |
| 3 | Sync/async | Async only |
| 4 | Types | An explicit `query[T]` verified by the checker; no codegen, no plugin |
| 5 | Nullability | SQLx's model with rowx's own corrected walker; unknown counts as nullable; `!`/`?` overrides |
| 6 | Param checking | Static, best effort, sqlty-style |
| 7 | Dynamic SQL | Static only in `query`; `query_unchecked` is the escape hatch |
| 8 | Row types | Tuples and scalars only; **amended (N1):** named row types are in scope, tuples and scalars are unchanged |
| 9 | Checker split | Python front-end with the Rust core doing DB work |
| 10 | Offline | None; a live DB is required |
| 11 | Diagnostics | Errors only, no fix |
| 12 | Methods | `fetch_one` (raises), `fetch_all`, `execute`; superseded by Q1 |
| 13 | No T | `T = None`; only `execute` (unchanged by Q1) |
| 14 | Runtime scope | A single connection only; **amended (C2):** a connection, a pool and transactions |
| 15 | Migrations | Out of scope; **amended (M1):** SQL-file migrations are in scope |
| 16 | Type map | asyncpg-like |
| 17 | Strictness | Exact match |
| 18 | Uncheckable queries | An error unless written as `query_unchecked` |
| 19 | T sources | Inline, or project aliases |
| 20 | Runtime validation | Arity plus NULL checks; **amended (N21, N22):** named rows also map names, and the NULL check names the field |
| 21 | sqlparser-rs | Not in v1 |
| 22 | Concurrency | One operation per connection; raise `ConnectionBusyError` |
| 23 | TLS | rustls |
| 24 | Packaging | One wheel and one CLI |
| 25 | Doc shape | Spec plus milestones and tasks |

## Review-driven decisions (revision 2)

| # | Decision | Why |
|---|---|---|
| R1 | `rowx-pg` is pure Rust; all PyObject work is in `rowx-py` | Keeps `cargo test` real and the boundary clean |
| R2 | Cancellation uses a Settling state, rowx's own CancelRequest read to EOF, and draining | Stale cancels killed 55% of follow-up queries |
| R3 | Base nullability checks `convalidated` and `relkind` | PG18 `NOT VALID` NOT NULL, foreign tables |
| R4 | The walker covers scan boundaries, grouping sets, RETURNING old/new, and every matching position | Five unsafe NOT NULL answers were measured |
| R5 | DSN pre-parse with custom rustls verifiers and the ring provider | tokio-postgres rejects `verify-*` and `sslrootcert` |
| R6 | Codec dispatch goes through tokio-postgres `Type`/`Kind` | Domains and arrays carry domain OIDs in params and elements |
| R7 | The encoder and checker share `accepted_params`; `text` accepts only `str`; no `str()` fallback; `str`→`date` is rejected | Otherwise parameter checking would be silently off for text |
| R8 | Retry-once is refined (routine match, 25P02 and 26000 handling) | Retries inside transactions fail |
| R9 | EXPLAIN runs over `simple_query`, only after Parse | The extended protocol demands values for `$n` |
| R10 | The safety wording admits plan-time function evaluation and adds `lock_timeout` | Accuracy |
| R11 | `Any`/`object` are nullability-neutral in both the checker and the runtime | The checker and runtime contradicted each other |
| R12 | `void` columns are treated as 0 columns, or match the tuple element `None` | `SELECT pg_sleep()` could not be satisfied |
| R13 | New rules: `param-count-mismatch`, `indirect-query`, `syntax-error`, `invalid-override` | Silent holes in "uncheckable queries are an error" |
| R14 | A normative source-map algorithm (`tokenize` + verification) | `ast` offsets are bytes and include escapes and seams |
| R15 | Compile eagerly with `TypeError`; fetch-time row-plan errors raise `InterfaceError` | Removes the class contradiction |
| R16 | One row-type grammar with a conformance test | Prevents the runtime and checker drifting apart |
| R17 | The adjacency rule inserts spaces around `$n` | `x{y}` would become the identifier `x$1` |
| R18 | Arity is checked against the statement before executing | Otherwise an empty result hides a shape mismatch |
| R19 | No `NoReturn` overloads | `query[Any]` would be typed `NoReturn` |
| R20 | macOS CI runs without the DB | Docker isn't available on macOS CI runners |

## Query operations (2026-10-25)

| # | Decision | Why |
|---|---|---|
| Q1 | `one`, `optional`, `all`, `fetch` and `execute`; a hard rename of `fetch_one`/`fetch_all`, with no aliases | sqlx's operation set under shorter names; 0.1.0 is unreleased |
| Q2 | `one` returns the first row; extra rows are not an error | Matches sqlx `fetch_one` |
| Q3 | `execute` returns `QueryResult(rows_affected)`, with no command tag | Room for more metadata; tokio-postgres doesn't expose the tag |
| Q4 | No `many`/`fetch_many`; `RowStream.result` carries the `QueryResult` | Postgres prepares one statement at a time; sqlx deprecated `fetch_many` (launchbadge/sqlx#3108) |
| Q5 | `fetch` returns an async iterator that is also an optional async context manager; the claim happens at the first `__anext__` | Plain `async for` stays simple; a stream that is never iterated holds nothing |
| Q6 | Early exit drains; only task cancellation sends a CancelRequest | sqlx drains to `ReadyForQuery`; a cancel would roll back `… RETURNING` DML |
| Q7 | `optional` on a nullable scalar is documented, not checked | asyncpg `fetchval` and SQLAlchemy `scalar_one_or_none` behave the same |
| Q8 | `fetch(conn, *, batch_size=1024)` | Tunes GIL crossings, like asyncpg `prefetch` and psycopg `itersize` |
| Q9 | Methods took `Connection`; **amended (C1):** they now take the `Executor` protocol, implemented by `Connection`, `Pool` and `Transaction` | YAGNI at the time; widening is backward compatible |

## Connections, pool and backends (2026-10-25)

| # | Decision | Why |
|---|---|---|
| C1 | `Executor` is a `typing.Protocol` over a private SPI (`_fetch`, `_stream`, `_execute`, `closed`); `rowx` never imports `rowx.postgres` | The widening that Q9 anticipated; `import rowx` loads no backend |
| C2 | The runtime covers a connection, a pool and transactions; pipelining stays deferred | Roadmap items 2 and 3 |
| C3 | Hard move: `rowx.connect`/`rowx.Connection` removed, no aliases; `rowx.ping` removed | 0.1.0 is untagged (the Q1 precedent) |
| C4 | Crates: `rowx-core`, `rowx-postgres` (was `rowx-pg`), `rowx-py-core`, `rowx-postgres-py`, `rowx-py` | A second backend only adds crates and a namespace |
| C5 | One extension module: `rowx._core` plus the `rowx._core.postgres` submodule, registered in `sys.modules`; `_core.pyi` becomes a stub package | One wheel (Q24 still holds) with a backend-shaped namespace |
| C6 | `type_table()`, `_types.py` and `_vocab.py` move under `rowx.postgres`; the checker stays in `rowx.check` and is Postgres-only | They are Postgres-specific; checker dispatch is deferred |
| C7 | Shared exception-class registry in `rowx-py-core`; each backend maps its own `thiserror` enum; replaces the "single mapping point" rule | Backends own their errors |
| C8 | A backend trait in `rowx-core` for cancel and is-alive, reused by the pool's `Manage` | `Cancel` held Postgres types |
| C9 | `ConnectOptions` is a frozen dataclass; `from_dsn` uses the Rust parser; `server_settings` travel in `options` as `-c`; other DSN keys go in `extra` | One parser; startup settings survive `RESET ALL`; nothing is dropped |
| C10 | `Connection` gains `ping`, `reset`, `transaction` and `detach` | sqlx and asyncpg parity |
| C11 | The pool is built by a time-boxed spike (#75): our own or `deadpool::managed` (settled by C25); `deadpool-postgres` is rejected | Its statement cache conflicts with rowx's cache and cancel/settle path |
| C12 | No IO in the constructor; the first acquire opens the pool; the pool is not loop-bound; fork use raises `InterfaceError`; background tasks hold `Weak` references | Safe at module level; no leaks |
| C13 | Acquire is FIFO for waiters and LIFO for idle connections; `PoolTimeoutError(InterfaceError)`; connect retries with backoff until `acquire_timeout` | Fairness and hot reuse (sqlx) |
| C14 | A cancelled waiter never runs its query (a cancelled flag checked after acquire) | pyo3-async-runtimes drops the future late |
| C15 | No round trip on the hot path; ping only after `ping_after`; no retry after an IO error | The query may not be idempotent |
| C16 | Leases: a generation check inside the claim CAS; release revokes first and settles a Busy connection | A stream can't run on another task's connection |
| C17 | `RESET_QUERY` is asyncpg's; `ROLLBACK` is prepended only when the "maybe in a transaction" flag is set; no `DISCARD ALL` | Avoids the 25P01 warning on every release and keeps the statement cache |
| C18 | A pool-wide schema epoch drops stale statement caches at checkout | A migration would otherwise abort the first transaction on every connection |
| C19 | Hooks (`init`, `setup`, `reset`) run on the caller's loop only; `init` is lazy; with no hooks, release runs in the background and `query.one(pool)` is one Rust future | Python callables can't run on background tasks; the fast path |
| C20 | Retirement by `max_lifetime`, `max_queries`, `idle_timeout` and `expire_connections()`; `min_size` is maintained in the background | asyncpg parity |
| C21 | `close(timeout)` and a synchronous `terminate()` | Bounded shutdown |
| C22 | Server-side poolers are documented, with no CI job | Cost |
| C23 | Transactions: `conn.transaction`, `pool.transaction` and `SAVEPOINT rowx_sp_<n>`; a Transaction is an Executor; the ending statement runs detached; a "failed" flag; an unknown `COMMIT` outcome discards the connection | asyncpg semantics without silent rollbacks |
| C24 | Pool performance bar: at least asyncpg `pool.fetchrow` at 64 tasks, non-gating | Matches the fetch benchmark approach |
| C25 | The pool is our own, in `rowx-core` (a FIFO-fair semaphore, a LIFO idle stack, a release task and a maintenance loop), not `deadpool::managed` (#75) | Both met every requirement; the own pool needs no new dependency, runs the checks where rowx wants them, and is faster |

## Type parity (#98)

| # | Topic | Decision | Why |
|---|---|---|---|
| T1 | Target | The set sqlx's `query!` type-checks, as built-in codecs. Records, multiranges, `xml`, `tsvector`, `pg_lsn`, `xid`, `reg*` and `jsonpath` stay unsupported | Bounded scope; records need named row types |
| T2 | Roadmap item 6 | Ranges, `inet` and `money` become built-in. Overrides and user codecs for domains and composites stay deferred | Built-ins need no override API |
| T3 | `inet`, `cidr` leaves | `IPv4Interface \| IPv6Interface` and `IPv4Network \| IPv6Network`, each one leaf and the only multi-member unions the grammar admits | The stdlib already models both; no new address type |
| T4 | `macaddr`, `macaddr8` | `str`, lower case, colon separated. `macaddr8` is beyond sqlx | No stdlib type |
| T5 | Ranges | `rowx.postgres.Range[T]`, a frozen dataclass modelled on psycopg; `T` is checked against the subtype; arrays of ranges included | Needs bounds, inclusivity and empty |
| T6 | `money` | `rowx.postgres.Money(minor_units)`, binary int8, with `to_decimal(scale)` taking the scale explicitly | Results are always binary, so a text path is not available; `lc_monetary` can change mid-session, so rowx holds no scale |
| T7 | Bit strings | `str` of `0`/`1` | Lossless and stdlib only |
| T8 | Interval months | `timedelta` stays and raises on months. `rowx.postgres.Interval` is an opt-in second leaf on the same column | No silent lossy conversion |
| T9 | Geometric | Frozen dataclasses in `rowx.postgres` | sqlx and asyncpg both have types |
| T10 | Extensions | `hstore` → `dict[str, str \| None]`, `ltree`/`lquery` → `str`, `cube` → `rowx.postgres.Cube(lower_left, upper_right)` | A flat tuple cannot tell a box from a higher-dimensional point |
| T11 | Extension matching | By name and owning extension (`pg_depend` → `pg_extension`), resolved in `describe.rs`, cached per connection with the statement cache, reused by runtime dispatch. Applies to `citext` too | A same-named user type must not be treated as the extension type |
| T12 | Wiring | `Matcher::ExtNamed` and `Matcher::RangeKind`, plus one `Codec` variant per family. Each feature PR calls these out | Enum change, not an appended row |
| T13 | Grammar | Each leaf needs a grammar line, a `_rowplan` rule, a resolver rule and conformance rows. Also admitted: `dict[str, str \| None]` (hstore) | Keeps both implementations in step |
| T14 | Parameters | Each type accepts the values in the [type table](type-mapping.md); subclasses are checked before their bases | Matches existing extraction order |
| T15 | Encoding | Binary where a `*_send` exists (results are always binary). `typsend = 0` rule unchanged | Existing rule |
| T16 | Testing | Every family joins the #94, #95, #108 and #109 oracles through `type_table()` | [Testing](testing.md) |

## Nullability precision (#111)

| # | Topic | Decision | Why |
|---|---|---|---|
| N1 | Scan-boundary mapping | A boundary output `x.col` is left alone only on an exact, unique name match to a plain, non-null-extended source column that resolves to the same base column as the describe origin (`table_oid`, `attnum`). Anything else promotes (the conservative rule) | The plan JSON has no alias list and prunes `Output`, so a name locates the candidate, but only the origin check proves identity (a swapped-alias CTE matches the wrong column by name) |
| N2 | Boundary edge cases | Computed outputs, duplicate names, planner NULL constants and missing entries promote. A twice-referenced `MATERIALIZED` CTE is mapped per alias | Each is a case where a name match could be wrong |
| N3 | Promote-only | The mapping can only remove promotions that the conservative rule would add; the walk never makes a column NOT NULL on its own | Keeps the existing soundness argument; gated by #109 |
| N4 | View verdicts | New `NullReason::ViewColumn`. A view column is NOT NULL only when its definition proves it | Replaces the blanket `ForeignOrView` for analysed views |
| N5 | Definition resolution | `pg_get_viewdef` and the describe both run under `SET LOCAL search_path = pg_catalog` in a rolled-back transaction. Building the query from `pg_rewrite` is rejected | A definition qualified for another `search_path` could resolve to a different table; `ev_action` has no supported text form |
| N6 | View column mapping | `attnum` maps to the output position by its rank among non-dropped columns | Dropped columns leave gaps in `attnum` |
| N7 | View fallbacks | Rules other than `_RETURN`, an unreadable `security_invoker` view, a cycle, depth over 8 and a definition that fails to describe are Nullable. `security_barrier` and `WITH CHECK OPTION` need none | They do not change the returned rows |
| N8 | Multi-statement invariant | The definition is concatenated into the simple-protocol `EXPLAIN` only after passing Parse as one statement, with the trailing `;` stripped; a test covers a crafted definition | Extends the existing security invariant |
| N9 | Spike #107 | No up-front decision; its recommendation returns as its own design change | Needs measurement first |

### Pool spike (#75)

Two prototypes ran the same 12 tests with paused time: `deadpool::managed` plus our layers, and a minimal own pool. Both
use a fake connection with a `Settling` phase. The code is on the unmerged branch `spike/75-pool-prototypes`.

| Requirement | `deadpool::managed` | Own pool |
|---|---|---|
| Recycle waits out `Settling`, then resets; a failed reset discards | Layered. `recycle` runs lazily on **acquire**, so the reset has to run in our release wrapper, which holds the `Object` until the reset ends and then drops it (or `Object::take`s it to discard). | Native: the release task settles, resets, then pushes to idle. |
| `min_size`, `idle_timeout`, `max_lifetime`, `max_queries`, `expire_connections()` | Layered. A `retain` pass must evict retired idle connections eagerly, because the lazy recycle would otherwise make a `get()` eat a stale one instead of creating (the first prototype failed on this). Timestamps are our own, since `Metrics` uses `std::time::Instant` and ignores `tokio::time::pause`. | Native, in one `maintain` function (reap, then fill). |
| FIFO waiters, LIFO idle | Yes (`QueueMode::Lifo`; waiters queue on a tokio semaphore). | Yes (tokio semaphore plus a `Vec`). |
| `close()` waits for checked-out connections | Layered: a counter and a `Notify`, shared by both. `Pool::close` fails waiters but doesn't wait. | The same counter and `Notify`. |
| Release is cancel-safe and works from `Drop` outside a runtime | Layered: the wrapper spawns through the stored `Handle`. | Native, the same way. |
| Lease check inside the claim, "init pending", caller-driven release | Not provided. The per-object hooks are `create`/`recycle`, which don't fit; every one of these is extra state around the `Object`. | Added directly where the connection is handed out. |
| New dependencies | `deadpool`, `deadpool-runtime` (and their transitives). | None. |

Layering `deadpool` took about 100 lines (release wrapper, `maintain`, close wait, generation check), which is within
the 150-line limit, so the requirements don't decide it. The deciding points are that the pool's real state machine
(leases, the settle wait, the reset and the epoch) lives outside `deadpool`'s `Manager` either way, that lazy recycling
puts work on the acquire path where rowx wants none, and that the own pool is smaller and faster.

Benchmark: `acquire` plus release, no reset (the release still goes through a spawned task in both), release build, 4
worker threads, `min_size = max_size = 10`, 400,000 operations, median of 5 runs, on the maintainer's macOS machine.

| Tasks | `deadpool::managed` | Own pool | Own / deadpool |
|---|---|---|---|
| 1 | 1,354,000 ops/s (739 ns/op) | 1,823,000 ops/s (549 ns/op) | 1.35 |
| 64 | 856,000 ops/s (1,168 ns/op) | 1,006,000 ops/s (994 ns/op) | 1.17 |

Both are far below a database round trip, so the speed is a tie-breaker only. #76 builds the own pool.

## Migrations (2026-10-29)

The design is in [Migrations](migrations.md).

| # | Topic | Decision | Why |
|---|---|---|---|
| M1 | Scope | SQL-file migrations are in scope: a `Migrator` in `rowx.postgres`, `rowx migrate` and `rowx database`, a migrations-aware `rowx check`, and an opt-in pytest plugin. Reverses non-goal "Migrations" and amends decision 15 | A project needs one tool for schema, queries and tests, and the checker needs a way to build the schema |
| M2 | File format | sqlx's: `<VERSION>_<DESCRIPTION>.sql`, or `.up.sql` plus `.down.sql`; `VERSION` is a positive `i64`; simple and reversible files can be mixed | An existing sqlx project switches without renaming files |
| M3 | File reading | Only regular files directly in the directory; a non-`.sql` file is ignored; a non-matching `.sql` file, a duplicate version, an unpaired up/down file and a non-UTF-8 file are errors naming the file; no warn or ignore setting | Matches sqlx's current default; silent skips hide mistakes |
| M4 | `-- no-transaction` | Byte-exact first line opts a file out of the per-migration transaction. The file is still one simple Query, which Postgres runs as one implicit transaction unless the file has its own transaction control, so `CREATE INDEX CONCURRENTLY` and similar must be alone in their file, and `ALTER TYPE … ADD VALUE` followed by a use of the value needs its own `COMMIT` in between | Needed for those statements; matches sqlx, which also sends the file as one Query |
| M5 | Checksum | SHA-384 over the raw bytes, no normalisation; `.gitattributes` `eol=lf` recommended; `ignored_chars` not ported | Checksums match sqlx byte for byte |
| M6 | Paths only | `Migrator(path)` takes a directory; package resources are deferred | Keeps the first version small |
| M7 | History table | `_rowx_migrations` with the columns of `_sqlx_migrations`; `table = "_sqlx_migrations"` adopts a sqlx project; a test runs sqlx rows through rowx | Adoption needs no data migration |
| M8 | Table quoting | `table` is parsed into one or two quoted identifiers; anything else is `ValueError` | sqlx interpolates the raw string |
| M9 | `create_schemas` | `CREATE SCHEMA IF NOT EXISTS` (quoted) before the table is created | As sqlx does |
| M10 | Layers | `rowx-core/src/migrate/` is pure (grammar, resolution, checksums, plan); `rowx-postgres/src/migrate.rs` carries a plan out; Python constructs, calls and maps errors | Keeps `cargo test` real, and a second backend reuses the plan |
| M11 | One claim per run | `run`, `undo`, `skip` and `info` claim one connection for the whole operation; a Pool acquires one; a `Transaction` raises `InterfaceError`; the GIL is released | Each migration manages its own transaction, and a `-- no-transaction` file can't run inside one |
| M12 | Locking | Session-level `pg_advisory_lock` with sqlx's key `0x3d32ad9e * crc32(current_database())`, taken before the table is created; `RESET_QUERY` is the backstop; `locking=False` turns it off | A sqlx migrator and a rowx migrator exclude each other |
| M13 | Executing a file | Simple query protocol through `Conn::simple`, the whole file as one Query message, with no splitting or parsing; internal, so there is still no public raw-SQL escape; migration files are trusted code | A migration is many statements |
| M14 | Transactional apply | `BEGIN`, the file, `INSERT` with `success = true` and `execution_time = -1`, `COMMIT`; `execution_time` is updated in nanoseconds afterwards (sqlx #1966); a file that commits itself must be `-- no-transaction`, and rowx doesn't parse to detect it | Recording inside the transaction makes apply atomic |
| M15 | Non-transactional apply | `INSERT` with `success = false`, the file, then `success = true`; a failure leaves a dirty row and the next run raises `DirtyMigrationError` | Improves on sqlx, which can't detect partial application on Postgres |
| M16 | Cancellation | Cancelling `run` cancels and settles as `close()` does; a transactional migration rolls back unrecorded, a non-transactional one is left dirty | Consistent with the driver's cancellation rules |
| M17 | Schema epoch | `run` or `undo` on a Pool that changes anything bumps the pool's schema epoch (C18) | No connection reuses a stale plan |
| M18 | Timeouts | Migrations don't change `statement_timeout` or `lock_timeout`; the guide covers `SET LOCAL` inside a transactional file, and connection settings (`server_settings`, DSN `options`, role defaults) for a file whose only statement can't run in a transaction block | Long migrations are legitimate; a `SET` can't share a Query with `CREATE INDEX CONCURRENTLY` |
| M19 | Validation order | Dirty, then missing locally (unless `ignore_missing`), then modified checksums, each before anything runs | No partial run on a bad history |
| M20 | Out-of-order | An error by default (`MigrationOrderError` naming both versions); `allow_out_of_order` applies in version order | Flyway's policy; sqlx doesn't offer it |
| M21 | `target` | `run(target=V)`: `V` must exist, a `V` below the highest applied is `MigrationOrderError`, up to and including `V`. `undo(target=V)`: every applied version above `V`, newest first, `0` means all; a missing `.down.sql` raises `MigrationSourceError` before anything reverts; `revert` with no target reverts the latest only | sqlx skips migrations without a down file silently |
| M22 | `skip` and `info` | `skip` baselines without running (sqlx `skip`). `info` returns applied, pending, modified, missing or dirty per version and creates nothing | sqlx's `info` creates the table, which its own FIXME calls a bug |
| M23 | Python surface | `Migrator(path, /, *, table, create_schemas, locking, ignore_missing, allow_out_of_order)`, constructed and validated synchronously; `Migration` and `MigrationStatus` are frozen dataclasses | A bad file fails at startup |
| M24 | Errors | `MigrateError(Error)` with `MigrationSourceError`, `MigrationChecksumError`, `MigrationMissingError`, `MigrationOrderError`, `DirtyMigrationError` and `MigrationFailedError` (`__cause__` is the `DatabaseError`); `thiserror` in core and postgres, Python exceptions only in `errors.rs` | Follows the error-mapping rule |
| M25 | CLI layout | `rowx.check.cli:main` stays the entry point; handlers in `check/migrate.py` and `check/database.py`; `[tool.rowx.migrate]` in `check/config.py` | One CLI, existing config errors |
| M26 | CLI behaviour | The URL is found as `check` finds it; `migrate add` infers versioning (sqlx's rule) and kind; run, revert and info print one line per migration and exit 0 or 2 | Matches `check`'s exit-2 class |
| M27 | `database` | Connects to `postgres` (`template1` for a database named `postgres`); `drop` and `reset` confirm on a TTY and need `-y` otherwise; `--force` is `WITH (FORCE)`; `setup` is create-if-missing then run | As `createdb` and sqlx-cli |
| M28 | Drift gate | With `[tool.rowx.migrate]`, `rowx check` exits 2 when any migration is pending, modified, missing or dirty; `--no-drift-check` skips it | A check against a stale schema gives wrong answers |
| M29 | `--scratch` | Creates `rowx_check_<hex>` from `template0`, migrates as the connecting role, checks read-only, drops `WITH (FORCE)` on every exit path; needs `CREATEDB`; no drift gate | Checks need no shared database |
| M30 | Safety statement | Amended: with `--scratch` the checker executes the migration files, only in the database it created; it still never executes user Python or checked queries | Accuracy |
| M31 | Test plugin | Opt-in `rowx.testing`, with no `pytest11` entry point; pytest is not a runtime dependency | Installing rowx never changes a pytest session |
| M32 | Test isolation | A session template `rowx_tpl_<hash>` over the (version, checksum) list, serialised under xdist, and a `CREATE DATABASE … TEMPLATE` clone per test dropped `WITH (FORCE)`; the template is closed before cloning | Fast, real databases, and templates reused until a migration changes |
| M33 | Fixtures | `rowx_database_url`, `rowx_connection` and `rowx_pool`, function-scoped, synchronous and driven by `asyncio.run`; tests run on asyncio (pytest-asyncio, or anyio's asyncio backend), not Trio | rowx objects aren't bound to one event loop, but its awaitables are asyncio futures |
| M34 | Plugin config | `rowx_database_url` ini option, else `database_url_env`; `--rowx-keep-templates`; stale templates removed by default | |
| M35 | Out of scope | Package resources, Python-code, repeatable and afterMigrate migrations, squashing, `sqlty breaking`-style checks (item 7) and per-test SQL fixtures | The design PR notes that item 7 gains a way to build old and new schemas |
| M36 | Dependencies | `sha2` for SHA-384 and a CRC-32 crate for the lock key | [Dependency evaluation](dependencies.md#dependency-evaluation) |
| M37 | Revert | Transactional: `BEGIN`, the `.down.sql`, `DELETE` the row, `COMMIT` (as sqlx). `-- no-transaction`: mark the row `success = false`, the file, then `DELETE`, so a failure leaves it dirty. The history row holds the `.up.sql` checksum | Mirrors apply; the checksum rule matches sqlx |
| M38 | Versions | *Amended after review (M2, M3).* `VERSION` is ASCII digits parsed as an `i64`: 0 or more, leading zeros allowed, so `1` and `001` are a duplicate; a sign or any other character is an error. Files are read following symlinks, and `.sql` is case-sensitive | sqlx parses any `i64` of digits; the first design said positive |
| M39 | `-- no-transaction` detection | *Amended after review (M4).* A byte prefix of the file (`starts_with`, as sqlx does), so a trailing `\r\n` or comment counts and a BOM disables it. It opts out of rowx's `BEGIN`/`COMMIT` only, the file stays one Query | Matches sqlx exactly; "first line" was inaccurate |
| M40 | Checksum and up/down pairing | *Amended after review (M5).* The checksum covers the raw bytes including a BOM; an unpaired `.up.sql` or `.down.sql` is an error where sqlx tolerates it, and the adoption guide says so | Byte-for-byte checksums; silent unpaired files hide mistakes |
| M41 | Table-name parsing | *Amended after review (M8, M9).* `table` and `create_schemas` follow Postgres identifier rules: an unquoted part matches `[A-Za-z_][A-Za-z0-9_$]*` and is lowercased, a `"…"` part is verbatim with `""` unescaped, at most 63 bytes per part, one or two parts; anything else is `ValueError` at construction. The executor always quotes each part | `MySchema.Migrations` must find the table sqlx created |
| M42 | Claim details | *Amended after review (M11).* The binding takes only a connection impl; a Pool is acquired with `async with pool.acquire()` so hooks and timeouts apply; a `Transaction`, or a `Connection` with an open transaction, raises `InterfaceError`; PgBouncer transaction mode is unsupported | Hooks, acquire timeout and reset behave normally |
| M43 | No lock for reads | *Amended after review (M12, M22).* `info` and dry runs take no advisory lock and create nothing; the key is the `bigint` overload with CRC-32/ISO-HDLC and a hard-coded sqlx test vector; `RESET_QUERY` is no longer the backstop | The drift gate must not wait behind a running deploy; `reset_query` may be `None` or custom |
| M44 | Transaction probe | *Amended after review (M14, M37).* After a transactional file (apply and revert), `SAVEPOINT rowx_probe`; a 25P01 means the file ended rowx's transaction, so rowx rolls back and raises `MigrationFailedError` before recording anything; otherwise it releases the savepoint. What the file itself committed stays committed, unrecorded, and the error says so | A file ending in `COMMIT` or `ROLLBACK` would otherwise record a migration that did not apply atomically |
| M45 | Cleanup and cancellation | *Amended after review (M16).* On error or cancellation: `ROLLBACK` when a step's transaction may be open, then `pg_advisory_unlock`; if either fails the connection is marked Broken (a pool discards it); a cancelled flag is checked before each step; only a migration file's own SQL becomes `MigrationFailedError` | No lock or transaction is left behind, and no step runs after the future is dropped |
| M46 | Cache clearing | *Amended after review (M17).* After any change the claimed connection's statement cache is cleared, and on a Pool the schema epoch is also bumped (`PgPool::bump_epoch`) | The claimed connection must not reuse a stale plan either |
| M47 | Run targets | *Amended after review (M21).* With `allow_out_of_order`, `run(target=V)` below the highest applied version is allowed; an unknown `V` is `ValueError` before any IO | Consistent with the out-of-order policy |
| M48 | `revert` replaces `undo` | *Amended after review (M21, M23).* `revert(executor, /, *, target=None, all=False, dry_run=False)`: `None` is the latest only, `target=V` every applied version above `V`, `all=True` everything; `target` with `all`, an unknown target and a target above the latest applied one are `ValueError`. A hard rename with no alias, as 0.1.0 is untagged. Supersedes `undo(target)` where `0` meant all, and the shipped `target: int | None` | One name matching the CLI, and no magic `0` |
| M49 | Revert plan rules | *Amended after review (M21, M24).* Plan targets are `Latest`, `Above(V)` and `All`, with `TargetNotFound` and `TargetTooNew`; a simple migration in range raises `IrreversibleMigrationError` before anything is reverted; an applied version in range that is missing locally is `MigrationMissingError` even with `ignore_missing`; `Missing` carries every missing version | sqlx skips such versions silently; a missing file can't be reverted |
| M50 | `MigrationResult` | *Amended after review (M23).* `run`, `revert` and `skip` return `tuple[MigrationResult, ...]` (`migration`, `elapsed`, which is `None` for a dry run or a skip); `skip` gains `dry_run`; `info` returns `MigrationStatus`. This replaces returning `MigrationStatus` from every method | A result and a status are different things |
| M51 | One `Migration` per version | *Amended after review (M23).* `Migration(version, description, kind: simple | reversible, path, down_path, checksum, down_checksum, no_transaction)`, one entry per version; `Migrator` exposes `path`; `MigrationStatus` gains `kind`, `local_checksum` and `applied_checksum` | Callers see a reversible pair as one migration |
| M52 | Typed errors | *Amended after review (M24).* `MigrationSourceError(path, version)` with subclass `IrreversibleMigrationError(version)`; `MigrationChecksumError(version, local_checksum, applied_checksum)`; `MigrationMissingError(versions)`; `MigrationOrderError(version, latest)`; `DirtyMigrationError(version)`; `MigrationFailedError(version, path, dirty, applied, error)` with `__cause__` set; each pickles and is re-exported from `rowx` | Callers and the CLI can act on the attributes |
| M53 | CLI parity | *Amended after review (M25, M26).* `migrate skip`; `revert --all` and `--ignore-missing`; `info --output-format json`; `--connect-timeout` (default 10) on `migrate` and `database`; `--source DIR` (cwd-relative, while `dir` is relative to `pyproject.toml`); `migrate add` prints a tip when `[tool.rowx.migrate]` is absent | Scripting, CI and sqlx-cli parity; `--source` is shipped behaviour the design omitted |
| M54 | Config keys | *Amended after review (M25).* `ignore_missing` and `allow_out_of_order` in `[tool.rowx.migrate]`, used by `rowx migrate`, the drift gate (`ignore_missing` lets `missing` pass), `--scratch` and the plugin; a flag beats the config | The same policy must apply everywhere migrations are applied |
| M55 | Maintenance database | *Amended after review (M27).* The URL's own database unless it is the target, then `postgres`, then `template1` (the target is `postgres`, or on 3D000 or 42501); a URL without a dbname targets the user name; a `WITH (FORCE)` privilege failure gets help text | Managed providers may lack or restrict `postgres` |
| M56 | `database` behaviour | *Amended after review (M27), recording shipped behaviour.* `create` on an existing database prints a message and exits 0; `reset` drops, creates and then runs every migration, as `setup` does; confirmation defaults to No and only `y`/`yes` confirms | Idempotent scripts; `reset` is useful only with a schema |
| M57 | Drift gate details | *Amended after review (M28).* It runs before any describe, whether or not queries were found, in one `connect_checker` session using `info`; a `MigrationSourceError` exits 2 naming the file; a role without `SELECT` on the history table exits 2 with a `GRANT` hint | A stale schema must be reported even with no queries; permission errors need an actionable message |
| M58 | Scratch tag and sweep | *Amended after review (M29).* The scratch name is `rowx_check_<16 hex>`, tagged `COMMENT ON DATABASE 'rowx:<pid>@<host>:<unix-ts>'`; at startup `rowx_check_*` databases over an hour old with no sessions are dropped, ignoring errors; migrations use config `ignore_missing` and `allow_out_of_order` | A SIGKILLed or cancelled run would otherwise leak its database |
| M59 | Template hash and build | *Amended after review (M32).* `rowx_tpl_<p>_<h>`, `<h>` is 16 hex characters of SHA-256 over the rowx version, migrations path, `table`, `create_schemas` and the sorted (version, checksum) list. An advisory lock keyed by `<h>`, taken in the maintenance database before looking for the template, is held while it is built as `rowx_tpl_<p>_<h>_tmp_<rand>` and renamed with `IS_TEMPLATE true, ALLOW_CONNECTIONS false`. Supersedes "closed before cloning" and the shipped completion comment | A database under the final name is complete by construction, and concurrent sessions build it once |
| M60 | Project-scoped cleanup | *Amended after review (M32, M34).* Every plugin database name carries `<p>`, 8 hex characters of SHA-256 over the resolved migrations path (`rowx_tpl_<p>_*`, `rowx_test_<p>_*`). Stale cleanup only touches the same project's templates; it sets `IS_TEMPLATE false` and uses a plain `DROP`, never FORCE. Clones and `_tmp_` builds are tagged with the `COMMENT`, and the project's ones over an hour old with no sessions are swept at session start | Other checkouts share the server, and `<h>` alone can't tell a project's older template from another project's |
| M61 | Plugin config | *Amended after review (M33, M34).* ini `rowx_database_url_env` (a variable name; replaces the literal `rowx_database_url` ini), `rowx_migrations` (overrides `dir`), `rowx_require_database` (fail instead of skip); `--rowx-keep-templates`; the template is an empty database with no migrations config | No password in `pytest.ini`; the repo's internal `ROWX_REQUIRE_DB` leaves the public surface |
| M62 | Fixtures and event loops | *Amended after review (M33).* Fixtures stay function-scoped and synchronous, but admin operations and the template build are blocking Rust calls on the shared runtime with the GIL released, never `asyncio.run`; async tests are supported on asyncio, including anyio's asyncio backend; Trio isn't. pytest-asyncio and pytest-xdist join the dev group, and `pytester` is enabled in the test conftest | Works from a running loop; the coverage must be real |
| M63 | Packaging | *Amended after review (M31).* A `pytest` extra (`rowx[pytest]`), and a ty `missing-direct-dependency` override scoped to `src/rowx/testing.py` | pytest stays optional |

## Named row types (2026-11-02)

Amends decision 8 (Row types) and closes roadmap item 1. T1's "records need named row types" still holds: composite and
record columns stay deferred, because a named type is allowed only as the row.

| # | Topic | Decision | Why |
|---|---|---|---|
| N1 | Scope | A query can declare its row as a `@dataclass`, `NamedTuple`, `TypedDict`, pydantic v2 `BaseModel` or pydantic dataclass, in addition to a tuple or a scalar. Amends decision 8; removes the "Named row types" non-goal | Roadmap item 1; the checker validates a field as it validates a tuple element |
| N2 | Where | `NAMED` appears at the `ROW` level only. A named type below the row (in a tuple, list, union, or as a field type) is `invalid-row-type` | Nesting needs composite and record support (T1) and a JSON story |
| N3 | Leaf order | Leaf detection runs before named detection in `_rowplan` and `resolve.py` | The `rowx.postgres` value classes are frozen dataclasses |
| N4 | Rejected on both sides | `Protocol`, `collections.namedtuple`, attrs, msgspec, `RootModel`, pydantic v1, a stdlib dataclass with `init=False`, `InitVar` (also on a pydantic dataclass, whose decorator accepts `init=False`), a nullable named row (help "use .optional()"), and a named type below the row | The runtime and the checker can both see them, or they have no sensible row |
| N5 | Checker-only rejections | A class decorator other than the dataclass decorators and `typing.final`, `metaclass=` or another unknown class keyword, and a hand-written `__init__` on a stdlib dataclass are `invalid-row-type`. The runtime accepts them and reports a failure as `RowConstructError` | The checker can't see what they do to the constructor; the runtime can just call it. Conformance rows record both verdicts |
| N6 | Matching | By name for dataclasses, TypedDicts and pydantic models. A NamedTuple matches by position, and the names must match too (`column-name-mismatch`) | Names are exact like everything else (decision 17); a positional-only match would hide a reordered SELECT |
| N7 | NamedTuple fields | Fields come from `_fields`, i.e. the class that directly subclasses `NamedTuple`. Annotations that a subclass adds are ignored, on both sides | The runtime has no other source. The checker mirrors it so the two agree |
| N8 | Column names | One function, `rowx-core::row_shape::column_name`, strips one trailing `!` or `?` and exempts `?column?`. `describe.rs` and the mapping call it; the checker passes raw names through `_core.postgres.map_columns`. Matching is case-sensitive | No Python port, so nothing is stripped twice or differently |
| N9 | Bijection with defaults | Every column needs a field (`unexpected-column`, even for `extra="allow"` and `extra_items`) and every field without a default needs a column (`missing-column`). A field is optional with a dataclass default, a pydantic default, `NotRequired` or `total=False` | An extra column is always a bug; this is stricter than sqlty |
| N10 | Duplicates | Two columns with the same stripped name are `duplicate-column` (`?column?` included, whether or not a field has the name; `field` is then `None`), as are two accepted names of one pydantic field. Their columns aren't also `unexpected-column`. One column matching two or more fields is `ambiguous-column` (pydantic only) | A dict or kwargs would silently drop one; pydantic keeps one of `id` and `user_id` silently |
| N11 | Not fields | `ClassVar`, `KW_ONLY`, `field(init=False)`, pydantic private attributes, `computed_field` and `Field(init=False)` on a pydantic dataclass (on a `BaseModel` it is an ordinary field, #206) | The constructor doesn't accept them |
| N12 | Field types | A field is exactly an `ELEM`, after unwrapping `Annotated`, `ReadOnly`, `Required` and `NotRequired`. A field typed `None` matches a `void` column | Same grammar and same rules as a tuple element |
| N13 | Annotations | Runtime: `get_type_hints(include_extras=True)`, and an unresolved name is `InterfaceError`. Checker: the class-body AST, with string annotations parsed. A `TYPE_CHECKING`-only name in a field, or in a row-level alias value, is `invalid-row-type` (help "import `X` at runtime"); only selected fields count | `get_type_hints` would fail on it at runtime, and ruff's TC003 produces exactly that import |
| N14 | Pydantic rows | *Amended 2026-11-03 (#206, #191 review).* Rust calls `__pydantic_validator__.validate_python(row_dict)` for a `BaseModel` and `cls(**row_dict)` for a pydantic dataclass, with a dict keyed by column name; an incomplete model gets `model_rebuild(raise_errors=False)` (`rebuild_dataclass(cls, raise_errors=False)` for a pydantic dataclass); still incomplete is `InterfaceError`. Needs pydantic 2.11 or later | Validators, constraints, aliases and `__init__` behave as in `model_validate`. This refines #181's "Rust calls `validate_python(row_dict)`" for pydantic dataclasses: that call raises `dataclass_exact_type` under `ConfigDict(strict=True)` (pydantic 2.14.2), while `cls(**row_dict)` runs the same validator through `__init__` and works. `RowConstructError` wraps the `ValidationError` the same way (owner decision). `RowShape(..., validate=True)` makes the validator call; `validate=False` calls the class, and only a pydantic dataclass uses it (#206) |
| N15 | Pydantic is exact | The checker reports lax coercions, and pydantic-only classes (`EmailStr`, `AnyUrl`, `SecretStr`) are `invalid-row-type` on both sides. Constraint aliases (`PositiveInt`, `constr()`) are checker-only rejections: the runtime strips `Annotated`. There is no pydantic type table | Decision 17. A table would be a second type map to keep in step |
| N16 | Pydantic names | A field's `str` `validation_alias`, else `str` `alias`, else its name; `validate_by_name`/`populate_by_name` adds the name; `validate_by_alias=False` keeps only the name. Config is read with `.get`. Literal class keywords are config and merge into it (the child wins) | The names pydantic accepts. Class keywords are how pydantic spells config |
| N17 | Unknown names (sqlty parity) | `AliasChoices`, `AliasPath`, a non-literal alias, or a before/wrap model validator makes a model's names unknown; so, in the checker only, do an `alias_generator` and an unreadable `model_config`. The four by-name rules are skipped with a `names not checked` note, and type and nullability checks still run on known names. At runtime every column goes in the dict and NULL checks cover only known names | A deliberate exception to "uncheckable is an error" (decision 18), accepted because these models are legitimate and sqlty accepts them. The exception is narrow and visible when a diagnostic is emitted. *Amended 2026-11-03 (#209, owner decision):* the `names not checked` note is attached only to emitted diagnostics, and an otherwise clean model with all-unknown names prints nothing. A per-field unknown next to known fields also notes when a diagnostic fires. A model validator in a mixin, an assigned `model_validator(...)(...)`, and config or validators in a conditional block count |
| N18 | Never imported | rowx never imports pydantic; it is detected through `sys.modules`. It is a dev-only dependency | `import rowx` loads no backend and has no runtime dependency |
| N19 | Generics | Generic classes and generic aliases are supported, substituted recursively through `__type_params__` and never `__value__[args]`, with an explicit argument count, inherited parameters rebound through `__orig_bases__`, and PEP 696 defaults. Unbound, `TypeVarTuple`, `ParamSpec` and wrong-count are `invalid-row-type`. This lifts the generic-alias rejection | Python doesn't check the argument count, and `__value__.__parameters__` follows the order of use |
| N20 | Runtime plan | `RowPlan` gains `kind="named"`, the field specs and a `_core.RowShape` cached per plan (constructor, form, matching, keys, accepted names, required, flags, type name) | One immutable description that Rust reads without calling back into Python |
| N21 | Mapping | `rowx-postgres` maps the prepared statement's columns in `open()` beside the arity check, including the cached-statement re-prepare. A mismatch is a `RowShapeError` before execution, even for an empty result. The mapping travels with the rows and sets the NULL mask; a pass-through column counts as nullable | Same guarantee as the arity check (R18) |
| N22 | Collect all errors | `row_shape::map_columns` returns every `ShapeError`. The runtime message lists them and the checker emits one diagnostic each | A SELECT with three problems shouldn't take three runs |
| N23 | `map_columns` binding | `_core.postgres.map_columns(columns, fields: list[(names \| None, required)], positional)` takes plain specs, not a `RowShape` | The checker can't build a `RowShape`: it has no constructor and never imports user code |
| N24 | Construction | Under the GIL, in one pass: one `PyTuple` for the positional form, a kwargs dict, the row dict, or the validator call. `_fetch` and `_stream` take a trailing `shape: RowShape \| None` | Keeps the tuple and scalar paths untouched |
| N25 | `RowConstructError` | `RowConstructError(DecodeError)` with `type_name`, a 0-based `row_index` across stream batches, and the original as `__cause__`. Types are not checked at runtime except by pydantic | A raising constructor is a decode failure of that row; stream semantics are `DecodeError`'s |
| N26 | Checker resolution | Classes are resolved from the project's `ModuleGraph`; bases and decorators match by resolved import and inheritance is project-only. An unsupported stdlib/typing/rowx/pydantic name is `invalid-row-type`; any other name outside the roots is `unresolved-row-type` | The checker never imports user code |
| N27 | Output | `note: field defined at path:line:col` and `"field": str \| null` in JSON; five new rules and a NamedTuple `arity-mismatch` range | A field-level diagnostic must say which field |
| N28 | Performance | Per-form variants in `bench/fetch_100k.py`, recorded with no hard threshold. The only gate is no regression in tuple and scalar paths | Named rows cost more by design |
| N29 | Out of scope | attrs, msgspec, `Protocol`, nested and JSON-shaped fields, composites and records (T1 stays deferred), `flatten`, a pydantic type table, named parameters, and row classes from installed packages | [Roadmap](roadmap.md) |
| N30 | Inheritance and calls | Fields follow the library's order, a redeclared field replaces the base's in place, and a non-row-type project base contributes no fields. The `keyword` form may call positionally when nothing is absent and no field is keyword-only | The checker mirrors what the library does; the positional call is an optimisation the benchmark decides |
| N31 | Messages and shape errors | `map_columns` returns field and column indices (`ShapeIssue`), and the caller renders the text, in the formats in api.md. `RowConstructError` wraps `Exception` only. The per-field type rules run on cleanly mapped columns of a by-name row and are suppressed by any positional mapping error | Goldens compare bytes; the binding and the checker hold different field records |
| N32 | `init=False` on a pydantic dataclass | `pydantic.dataclasses.dataclass` takes only `init=False` (meaning "keep pydantic's `__init__`"); `init=True` raises. A literal decorator `init=False` is accepted on both sides, and only an `InitVar` field is rejected. Amends N4. Dated 2026-11-03 | Verified on pydantic 2.14.2 in #206; the design's "rejected on both sides" couldn't hold |
| N33 | Pydantic dataclass call | `RowShape` takes `validate`: `validate_python(dict)` for a `BaseModel`, the class call `cls(**dict)` for a pydantic dataclass. Refines N14 (owner decision, #191 review round 2; #206). Dated 2026-11-03 | `strict=True` pydantic dataclasses reject a dict with `dataclass_exact_type` |
| N34 | `TYPE_CHECKING` at row level | A row-level alias whose value names a `TYPE_CHECKING`-only import is `invalid-row-type` (help "import `X` at runtime"), matching the runtime's `InterfaceError`. Amends N13 and checker.md's "aliases used as `T` keep today's rule" (owner decision, #207). Dated 2026-11-03 | The runtime always fails to build that plan |
| N35 | Pydantic config precedence | Name config mirrors pydantic's `_build_effective_config` (explicit `validate_by_name`, then `populate_by_name`, then `validate_by_alias=False`); `class Config: allow_population_by_field_name` is ignored. Model config merges direct bases left to right, then own config, then class keywords; a pydantic dataclass's config does not merge. Plain mixins contribute annotations and before/wrap validators (#209). Dated 2026-11-03 | The checker must agree with pydantic 2.14, which it never imports |
| N36 | More checker-only rejections | `Json[...]`, mixed model/stdlib/pydantic dataclass forms, an undecorated subclass of a pydantic dataclass, external stdlib bases (`abc.ABC`, `Exception`), fields in a conditional block and a plain alias over a `TypeVarTuple`. Extends N5 (#207, #209, #211). Dated 2026-11-03 | The runtime accepts them; the checker can't see through them. Conformance rows record both verdicts |
| N37 | Partial mapping | On failure `map_columns` returns a partial mapping (by name each column's sole matching field; by position all `None`), and the checker runs type and nullability rules on cleanly mapped columns, skipping those named in duplicate or ambiguous issues (#204, #207). Dated 2026-11-03 | Refines N31 |
| N38 | Generic details | Parameters are collected depth first over `__orig_bases__` (`Generic[...]` overrides), imported `TypeVar`s match by identity, PEP 696 defaults may refer to earlier parameters, old-style generic aliases work, and a bare plain generic alias is rejected. Only `type` aliases, class substitutions and re-exports count as hops; generic class chains cap at 16 subscripted bases. Refines N19 (#211). Dated 2026-11-03 | The checker mirrors how `typing` collects parameters; the 16-hop limit stays shared |
| N39 | Note paths | The paths inside notes are absolute for now; relativizing them is follow-up #208. Dated 2026-11-03 | Out of scope for this feature |

## Security hardening (2026-11-03)

Records the decisions of #214 (design #215). The threat model and invariant are in [Security](security.md#threat-model).

| # | Topic | Decision | Why |
|---|---|---|---|
| S1 | `:ident` normalisation | A name is accepted if it is any `str` instance (`StrEnum` included), normalised once with `str.encode` (strict UTF-8, so a lone surrogate is `TypeError`); every check and the quoting run on the bytes, and nothing is called on the value again. Tuples are iterated with `tuple.__iter__`. C0 and C1 control characters are rejected | A `str` subclass that overrides `__contains__`, `__len__`, `encode` or `replace` bypassed the NUL check, the 63-byte limit and the quoting (F1) |
| S2 | Identifiers stay unrestricted | **Rejected:** enum-only identifiers. `{x:ident}` stays `query_unchecked`-only, and the guide documents allowlisting | Quoting is safe for any name; which name is allowed is an application decision, and an enum-only rule would remove legitimate dynamic identifiers |
| S3 | Runtime `ident` spec | There is no separate path: `{x:{spec}}` evaluating to `"ident"` can't be told apart at runtime. The checker inspects the interpolations of `query_unchecked` calls and reports a non-literal format spec as `dynamic-query`, while still skipping their SQL | The runtime has one code path, and the static rule closes the gap |
| S4 | Compile limits | A NUL in the compiled SQL and more than 65535 parameters after flattening are `TypeError`s before any IO, with fixed messages | Both failed closed before, but with misleading `InterfaceError` messages and no test (F6) |
| S5 | Hand-built templates | The checker rule `template-construction`, over every file under the include roots, documented as best-effort. Runtime unchanged | The only raw-SQL path is `Template(...)` with a non-literal string, and the checker is where rowx already enforces `dynamic-query` (F2) |
| S6 | Rejected: runtime provenance | **Rejected:** matching `template.strings` by identity against the caller's `co_consts` | It works on CPython 3.14, but it breaks `t"a" + t"b"` and templates built in helpers, and it costs a frame walk |
| S7 | Rejected: `assert_sql_safe` | **Rejected:** an explicit raw-SQL escape hatch (as in sqlx's `AssertSqlSafe`) | It would reverse "there is no public raw-SQL escape" |
| S8 | Arrays and ranges are always binary | Both `encode_literal` functions are removed. Binary element encoders cover `numeric`, `timetz`, `json`, `jsonb`, enum labels and `uuid` from `str`. `TYPE_TABLE` is untouched | With no text literal there is no escaping rule for a client-only encoding (SJIS, BIG5, GBK) to defeat (F5). It makes the invariant structural instead of tested |
| S9 | Open question: scalar `numeric` and `timetz` | **Answer: no, scalars stay text.** | A scalar has no literal grammar, so a text-format scalar can corrupt only itself and can't change structure. Moving it would change `encode_format` rows in the append-only `TYPE_TABLE`, widen the change, and risk the benchmark for no security gain. It can be revisited for consistency |
| S10 | Encoding watch | A backstop, landing after the binary encoders: `poll_message` is checked on every poll including `Pending`; `client_encoding != UTF8` or `standard_conforming_strings = off` taints the connection through `client()`/`parts()`, and the pool discards it through `is_broken`. The residual window and the cost of a legitimate `SET standard_conforming_strings = off` are documented. `DateStyle` and `IntervalStyle` are not watched | The drift is silent data corruption. Binary arrays are what stop element injection, so the window can only cause drift. All decoders are binary, so the other two settings don't matter |
| S11 | Open question: `DISCARD TEMP` | **Answer: add it.** `RESET_QUERY` becomes `SELECT pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; RESET ALL; DISCARD TEMP;` | `pg_temp` is searched first for relations, so one checkout's temp table shadows an application table for the next (F10). This diverges from asyncpg's reset query, which leaves temp tables. It runs inside the multi-statement simple query (verified on Postgres 18) |
| S12 | Rejected: `DEALLOCATE ALL` in the reset | **Rejected.** SQL-level `PREPARE` is documented as unsupported across checkouts, and `reset_query=None` as unsafe for multi-tenant pools | rowx's own protocol-level statements are cached per connection and would be destroyed |
| S13 | Internal SQL | Every statement rowx builds names `pg_catalog` objects explicitly (`pg_catalog.now()` in the history DDL) | A role or DSN `search_path` could pick which `now()` runs (F11) |
| S14 | Option validation | `server_settings` keys match the GUC grammar (63 bytes per segment), `extra` keys match `^[a-z_]+$`, and a NUL or C0 character in any value is an `InterfaceError` that names the key and not the value. `extra` values are redacted in `ConnectOptions.__repr__`, and the userinfo of an echoed DSN is redacted | A tab or newline in a key was split into extra startup options (F3), and `repr` printed secrets (F4) |
| S15 | TLS and auth | `channel_binding` is a typed `Literal["disable", "prefer", "require"] \| None` option; `require` with `sslmode=disable` is a parse-time `InterfaceError`. Production use is `sslmode=verify-full&channel_binding=require` | Under `prefer` a MITM can ask for a cleartext or MD5 password (F9), and `channel_binding=require` blocks it |
| S16 | Rejected: changing the `sslmode` default | **Rejected.** `prefer` stays | Parity with libpq, sqlx and asyncpg; a change would break connections to servers without TLS |
| S17 | Decoders under an untrusted server | `#![deny(clippy::indexing_slicing, clippy::arithmetic_side_effects)]` on `wire/` only; a fuzz target for every decoder with `-rss_limit_mb`; a panic-boundary test; a RUSTSEC-2024-0363 regression (an ignored 2 GiB test) | A MITM controls these bytes under the default `sslmode`, and a panic is a `BaseException` in Python (F8) |
| S18 | Checker EXPLAIN | **Conditional on #229**, the last and severable sub-issue: prepare, find the name in `pg_prepared_statements`, and `EXPLAIN … EXECUTE <name>(NULL, …)` under `plan_cache_mode = force_generic_plan`. A parity run over the `fuzz-nullability` corpus must give identical verdicts; otherwise keep concatenation and assert one result set and one `CommandComplete`. Until #229 lands, the concatenation text in `nullability.md` is authoritative | Defence in depth (F7), and the riskiest change for the least gain |
| S19 | Non-goals | A `LIKE`-escape helper, `require_auth` (needs tokio-postgres support), cargo-deny, pip-audit, `SECURITY.md`, client certificates (roadmap item 8) and the embedded proxy's socket checks (#197) | Out of scope for this feature, or blocked on code that hasn't landed |

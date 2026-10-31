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
| 8 | Row types | Tuples and scalars only |
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
| 20 | Runtime validation | Arity plus NULL checks |
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

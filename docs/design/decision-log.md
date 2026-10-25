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
| 15 | Migrations | Out of scope |
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
| C11 | The pool is built by a time-boxed spike (#75): our own or `deadpool::managed`; `deadpool-postgres` is rejected | Its statement cache conflicts with rowx's cache and cancel/settle path |
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

# Overview

## Goals and non-goals

### Goals (v1)

1. **Runtime API.** An async Python API runs t-string SQL against Postgres and decodes rows
   into the declared tuple or scalar type `T`.
2. **Owned driver.** A Rust core (PyO3, abi3-py314) owns the connection. It is built on
   `tokio-postgres`.
3. **Static checker.** `rowx check` finds every `query[T](t"...")` call. For each call it:
   - prepares the SQL on a live Postgres 18 database. A query the server rejects is an
     error.
   - compares the described result columns with `T`: count, Python type and nullability
     must match **exactly**.
   - compares interpolated values whose Python type is statically evident with the
     parameter types the server inferred (as sqlty does).
4. **Nullability** follows SQLx's model: catalog NOT NULL, plus a plan walk for outer joins.
   Unknown columns count as nullable, and `"col!"`/`"col?"` overrides apply. rowx uses its
   own corrected implementation.
5. **One source of truth** for the type mapping. The decoder, the encoder and the checker all read the same table
   ([Type mapping](type-mapping.md#type-mapping-single-source-of-truth)), and one row-type grammar
   ([Row-type grammar and runtime row plan](api.md#row-type-grammar-and-runtime-row-plan)) serves both the runtime and
   the checker.

### Non-goals for v1

These are deferred, not rejected; [Deferred work](roadmap.md#deferred-work-by-expected-value) lists them in priority
order.

- Backends other than PostgreSQL. Only Postgres **18** is supported and tested.
- A sync API, and the free-threaded build (`3.14t`); abi3 wheels don't install there.
- Pipelining, LISTEN/NOTIFY and COPY. (A connection pool and a transaction helper are in scope; see
  [API](api.md#rowxpostgres).)
- An offline cache. The checker always needs a live database.
- `--fix` and rewriting, warnings, severities and ignore comments. Every diagnostic is an
  error.
- Named row types (dataclass, TypedDict, NamedTuple, Pydantic, Protocol). Only tuples and
  scalars are supported.
- Multi-statement execution (sqlx's `fetch_many`).
- Migrations.
- A client-side SQL parser ([Dependency evaluation](dependencies.md#dependency-evaluation)).
- Type-checker plugins.
- libpq environment variables (`PG*`) and `.pgpass`.

## Background and prior art

| Tool | Approach | What rowx takes from it |
|---|---|---|
| **SQLx** (Rust) | Proc macros Parse/Describe on a live DB, with `pg_attribute` + `EXPLAIN` nullability and an offline `.sqlx` cache. | It validates against a live DB. rowx takes its nullability model, the `!`/`?` overrides, its reasons for not parsing SQL (FAQ), and its method names. |
| **sqlty** (Py+Rust) | Offline sqlparser-rs analyser over DDL, with `SQL[T]` annotations. | rowx takes the annotation as the contract, static argument types (`_params.py`), docs-as-tests and fidelity tests, and `query[T]` as a **class**. Its config ergonomics come from `sqlty/_config.py`. |
| **SafeQL** (TS) | ESLint check of tagged templates against a live DB. | It is the closest prior art, and it shows the approach works. |
| **psqlpy** | A PyO3 driver on tokio-postgres + deadpool. | It shows the stack works in production. rowx avoids its binary-only parameter bugs ([Codec](driver.md#codec-rowx-postgres-pysrccodec-rowx-postgressrcwire)). |
| **sql-tstring, t-sql** | Turn t-strings into parameterised SQL, with no checking. | The niche of checking t-string SQL against a live DB is open. |

The research reports, spike code and review reports are in the design session's scratchpad. They are not part of the
repo. The key evidence is reproduced in
[Evidence: why not sqlx's describe](nullability.md#evidence-why-not-sqlxs-describe) and
[Dependency evaluation](dependencies.md#dependency-evaluation).

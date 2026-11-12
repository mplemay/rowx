# Changelog

## 0.4.0

- `rowx[embedded]` adds an in-process Postgres 18 for development, checking and tests. In code,
  `rowx.embedded.Database` is a pool-shaped executor that owns one instance (`Database()` in memory,
  `Database("/abs/dir")` persistent). An `embedded://` URL selects it for `rowx check`, `rowx check-compat`,
  `rowx migrate` and the pytest plugin; generic `connect`, `Pool` and `ConnectOptions.from_dsn` do not accept one. All
  of it lives in `rowx-embedded`, and `rowx-postgres` does not know about it. See
  [Embedded Postgres](guide/embedded.md).
- `rowx-embedded` releases in lockstep with the other distributions (0.4.0, from the `v*` tag and `release.yml`)
  instead of as `18.4.0` on its own `embedded-v*` tag. It depends on the matching `rowx-postgres`, and `rowx[embedded]`
  pins `rowx-embedded==0.4.0`. The engine is still PostgreSQL 18.4. This tree is version 0.4.0. 0.4.0 is not on PyPI
  until the `v0.4.0` tag is published.
- A pool acquire that is retrying a connect now takes a connection that another caller returns meanwhile. Before, it
  kept retrying until its timeout, so a pool whose server was at its connection limit could time out next to an idle
  connection of its own.
- A pool acquire that times out while connecting now raises `PoolTimeoutError` with the last connect error as its
  `__cause__`, and the message says so, instead of reporting the timeout alone.
- `rowx`, `rowx-core`, and `rowx-postgres` drop the deprecated `License :: OSI Approved :: MIT License` classifier.
  The license stays `MIT` in the `license` field.

## 0.3.0

- `_fetch` / `_stream` take a core `ShapeSpec`, and the Postgres executor builds and caches the native shape.
- Three lockstep distributions replace the published 0.2.0 wheel. `rowx-core` (import `rowx_core`) has no runtime
  dependencies. `rowx-postgres` (import `rowx_postgres`, native module `rowx_postgres._core`) depends only on matching
  `rowx-core`. `rowx` (import `rowx`) depends on matching `rowx-core`, and `rowx[postgres]` installs matching
  `rowx-postgres`. Published as `v0.3.0`.
- `query(...)` captures a neutral template and selects no compiler. Each call uses that executor's `compiler`. A
  later call on another executor compiles again.
- The `query` constructor rejects a non-template, a NUL in static text, a conversion, an unsupported format spec,
  and an `:ident` value that is not a non-empty `str` (or a tuple of them) without a NUL or a control character.
  When the method runs, `rowx_postgres._compiler` emits `$n` and the adjacency space, quotes the captured
  identifier, and raises `TypeError` for an `:ident` outside plain code, a name longer than 63 bytes, and more
  than 65535 parameters.
- `rowx` re-exports the query API, and `rowx.postgres` is the public driver import for the `rowx` distribution.
  `from rowx import query` is the same object as `from rowx_core import query`. `from rowx.postgres import connect`
  is the same object as `from rowx_postgres import connect`. `rowx.core` re-exports only the `rowx_core` names that
  `rowx` and `rowx.errors` do not already export. A runtime-only install still imports `rowx_core` and
  `rowx_postgres`.
- Each exception class is defined in `rowx_core.errors` and re-exported from `rowx` and `rowx.errors` as the same
  object. The native error registry uses those classes. There is no Postgres-specific exception tree.
- `rowx check --coverage` lists recognized direct query sites and whether each was described and compared.
  `--inspect` prints each described statement's columns, nullability, and parameter types, and does not execute the
  queries. With `--output-format json`, coverage and inspect go to stderr and diagnostics stay on stdout.
  `--require-checked` adds `coverage-require-checked` when no query was successfully described.
  `--disallow-unchecked` adds `coverage-unchecked` for every recognized `query_unchecked` call.
- `rowx check-compat --against BASE --head HEAD` checks the same sources at two revisions. The URL is
  `--database-url` or `ROWX_DATABASE_URL`. It does not read `database_url_env`. Exit 0 is a complete compatible
  subset, exit 1 is an incompatibility, and exit 2 is incomplete.

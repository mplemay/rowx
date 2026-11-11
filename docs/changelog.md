# Changelog

## 0.3.0 (unreleased)

- `_fetch` / `_stream` take a core `ShapeSpec`, and the Postgres executor builds and caches the native shape.
- Three lockstep distributions replace the published 0.2.0 wheel. `rowx-core` (import `rowx_core`) has no runtime
  dependencies. `rowx-postgres` (import `rowx_postgres`, native module `rowx_postgres._core`) depends only on matching
  `rowx-core`. `rowx` (import `rowx`) depends on matching `rowx-core`, and `rowx[postgres]` installs matching
  `rowx-postgres`. The packages in this tree are still version 0.2.0 until #270. 0.3.0 is unreleased and is not on PyPI.
- `query(...)` captures a neutral template and selects no compiler. Each call uses that executor's `compiler`. A
  later call on another executor compiles again.
- The `query` constructor rejects a non-template, a NUL in static text, a conversion, and an unsupported format
  spec. PostgreSQL `$n` placeholders, adjacency padding, quoting, lexical placement, and the 65535 parameter cap
  are raised when the method runs, in `rowx_postgres._compiler`.
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

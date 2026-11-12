# rowx user guide

You write the SQL, name the row in ordinary Python, and `rowx check` fails the build when Postgres disagrees.

The `python test` blocks in this guide run against a seeded database (users `alice`, `bob`, and `carol`, plus
`orders`). The test suite sets `DSN`.

## Start

- [First query](guide/get-started.md): install `rowx[postgres]` and run `SELECT`.
- [Rows](guide/rows.md): a scalar, a tuple, or a class you already write.
- [Check](guide/checker.md): the diagnostic when a column and `T` disagree.
- [From SQLAlchemy](from-sqlalchemy.md): what you give up, and what you keep.

## The rest of the job

- [Queries](guide/query.md): one statement, bound values, and `query_unchecked`.
- [Parameters](guide/parameters.md): `ANY`, casts, and nullability overrides.
- [Executors](guide/executors.md): a connection, a pool, a transaction.
- [Streaming](guide/streaming.md): `fetch` when the result is large.
- [Errors](guide/errors.md): `RowNotFound`, `RowShapeError`, and `DecodeError`.
- [CI](guide/ci.md): run the checker on a throwaway Postgres.
- [Migrations](guide/migrations.md): SQL files, applied in order.
- [Testing](guide/testing.md): a migrated database per test.
- [Security](guide/security.md): what is trusted, and what stays a bound value.

## Known limits

Each case is pinned in [known limits](known-limits.md). The short index:

- **Nullability inference** is conservative, never optimistic. Views, set operations, recursive CTEs and computed
  columns are nullable; use `"col!"` to narrow. See [known-limits.md](known-limits.md) for each case.
- **Unsupported types** (multiranges, composites, `xml`, `hstore`, `regclass`, ...) are
  `unsupported-type` in the checker and raise `DecodeError` at runtime. Cast to `text`.
- **Decode errors.** `date`, `timestamp` and `timestamptz` that are infinite or outside years 1 to 9999, and `interval`
  values with a months component, raise `DecodeError`. Cast to `text`.
- **Array elements.** Element types and nullability inside `list[...]` are not checked at runtime; a NULL element
  decodes to `None`.
- **One operation per connection.** There is no pipelining yet. An open `fetch` stream holds the connection until it
  ends (on a pool, until it releases the lease).
- **No multi-statement execution** and **no command tag** in `QueryResult`. See [known-limits.md](known-limits.md).
- **Cancellation.** A cancelled statement inside a transaction aborts it (25P02), and a future cancelled during prepare
  can leak a named server statement until the connection closes.
- **Schema changes.** If the schema changes inside a transaction, a retried statement can fail with 25P02. `ROLLBACK`
  and retry.
- **Named row types** support dataclasses, NamedTuple, TypedDict and pydantic. attrs, msgspec, nested row classes and
  type overrides (`"col: Type"`) are not supported. See [What isn't supported](guide/rows.md#what-isnt-supported).
- **`float` parameters.** An `int` above 2^53 bound to a `float4`/`float8` parameter loses precision.

# rowx user guide

rowx runs raw SQL written as t-strings and returns typed rows. The same row types drive `rowx check`, a static checker
that prepares every query against a live Postgres 18 database and reports mismatches.

The `python test` blocks in this guide are executed against a seeded database (users `alice`, `bob` and `carol`, plus
`orders`) by the test suite, with `DSN` set to its connection string.

- [Get started](guide/get-started.md): install, a first query, and the upgrade from 0.2.
- [Queries](guide/query.md): what `query` and `query_unchecked` accept.
- [Parameters](guide/parameters.md): binding rules and nullability overrides.
- [Rows](guide/rows.md): scalar, tuple, and named rows.
- [Executors](guide/executors.md): connections, pools, transactions, and the statement cache.
- [Streaming](guide/streaming.md): `fetch` and `RowStream`.
- [Errors](guide/errors.md): exception identity, and when errors are raised.
- [Checker](guide/checker.md): `rowx check` against an explicit database target.
- [CI](guide/ci.md): run the checker on a throwaway Postgres.
- [Migrations](guide/migrations.md): SQL-file migrations.
- [Testing](guide/testing.md): the `rowx.testing` pytest plugin.
- [Security](guide/security.md): what to trust, and what stays a bound value.

## Known limits

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

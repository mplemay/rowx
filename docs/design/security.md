# Security and safety

- Values are always bound as server-side parameters, never interpolated into SQL text.
  - `query` rejects every dynamic construct.
  - In `query_unchecked`, nested templates also bind their values, and `:ident` is quoted
    and length-checked.
  - There is no public raw-SQL escape.
- **Migration files are trusted code.** `Migrator` and `rowx check --scratch` run their statements over the simple query
  protocol, because a file holds many statements. This is internal, and it adds no public raw-SQL escape. Only the
  table name is built into SQL by rowx, and it is parsed into quoted identifiers
  ([History table](migrations.md#history-table)); so are database names in `rowx database` and `--scratch`.
- The checker's guarantees and limits are in
  [Interface and checker session](nullability.md#interface-and-checker-session), including plan-time evaluation of
  functions and locking, and the amended statement for `--scratch`, which executes migration files in a database it
  creates. The docs recommend a dedicated role on a CI or disposable database.
- The checker's EXPLAIN runs over the simple protocol only after Parse has accepted the
  SQL as a single statement ([Algorithm](nullability.md#algorithm), step 1). This invariant is tested.
  - A view definition is held to the same rule: it is described under a pinned `search_path` and concatenated only
    after Parse accepts it as one statement ([View columns](nullability.md#view-columns)).
- Neither diagnostics nor exceptions may include a DSN password. Parameter values are
  redacted from Rust debug logs ([Codec](driver.md#codec-rowx-postgres-pysrccodec-rowx-postgressrcwire)).
- TLS defaults to `prefer`, which is encrypted but not verified, as in libpq. The docs
  recommend `verify-full`.

# Security and safety

- Values are always bound as server-side parameters, never interpolated into SQL text.
  - `query` rejects every dynamic construct.
  - In `query_unchecked`, nested templates also bind their values, and `:ident` is quoted
    and length-checked.
  - There is no raw-SQL escape.
- The checker's guarantees and limits are in
  [Interface and checker session](nullability.md#interface-and-checker-session), including plan-time evaluation of
  functions and locking. The docs recommend a dedicated role on a CI or disposable database.
- The checker's EXPLAIN runs over the simple protocol only after Parse has accepted the
  SQL as a single statement ([Algorithm](nullability.md#algorithm), step 1). This invariant is tested.
- Neither diagnostics nor exceptions may include a DSN password. Parameter values are
  redacted from Rust debug logs ([Codec](driver.md#codec-rowx-pysrccodec-rowx-pgsrcwire)).
- TLS defaults to `prefer`, which is encrypted but not verified, as in libpq. The docs
  recommend `verify-full`.

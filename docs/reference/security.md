# Security

## Threat model

| Trusted (authored by the developer or operator) | Untrusted |
|---|---|
| static t-string text, migration files, `reset_query`, the DSN and `ConnectOptions`, the checker's database ([plan-time evaluation](nullability.md#interface-and-checker-session)) | interpolated values, `{x:ident}` values, and the **types** of those values (`str` and `tuple` subclasses) |
| | server bytes when `sslmode` is weaker than `verify-full` (a MITM can impersonate the server) |
| | session state that application SQL can change (`SET client_encoding`, `SET ROLE`, temp tables) and that outlives a pool checkout |

Invariant: **no untrusted input can change the parse tree of a statement, or the element and bound structure of an
array or range parameter.**

## Parameters and SQL text

- Values are always bound as server-side parameters, never interpolated into SQL text. `query` rejects every dynamic
  construct. In `query_unchecked`, nested templates also bind their values, and `:ident` is quoted and length-checked
  ([`:ident`](api.md#ident)). There is no public raw-SQL escape.
- **`:ident` values are untrusted, and so are their types.** The compiler normalises a name once, with `str.encode`,
  and every check and the quoting then run on those bytes, so a `str` or `tuple` subclass that overrides
  `__contains__`, `__len__`, `encode`, `replace` or `__iter__` has no effect. Identifiers are **not** restricted to
  enum members: an application that takes a column name from a user **MUST** allowlist it.
- **Compile limits** are `TypeError`s raised before any IO: a NUL in the SQL text, and more than 65535 parameters
  after flattening ([Compile limits](api.md#compile-limits)).
- **Hand-built templates.** The only way to put attacker-controlled text into the static part of a query is
  `Template(...)` with a non-literal string (or `t"…" + Template(user_str)`). The runtime compiles whatever `Template`
  it is given, and has no provenance check. The checker's `template-construction` rule
  ([Rules](checker.md#rules)) rejects that construction in every file under the include roots, and also a non-literal
  format spec in any t-string literal there.
  - The rule is **best-effort**. `getattr` and `eval` can still build a template, so can code outside the include
    roots, and so can generated code. The `template-construction` rule can be configured or suppressed with a native
    rowx ignore comment; doing so hides the finding and does not add a runtime provenance check. See
    [Rules](checker.md#rules).
  - A runtime provenance check and an `assert_sql_safe` escape hatch were both rejected.
- **Migration files are trusted code.** `Migrator` and `rowx check --scratch` run their statements over the simple
  query protocol, because a file holds many statements. This is internal, and it adds no public raw-SQL escape. Only
  the table name is built into SQL by rowx, and it is parsed into quoted identifiers
  ([History table](migrations.md#history-table)); so are database names in `rowx database` and `--scratch`.
- **Internal SQL names `pg_catalog` objects explicitly** (`pg_catalog.now()` in the history DDL,
  `pg_catalog.pg_advisory_unlock_all()` in `RESET_QUERY`, and so on). Operators are qualified as
  `OPERATOR(pg_catalog.…)` too, because operators resolve through `search_path` like functions. A role or DSN
  `search_path` cannot make rowx's own statements resolve to a shadowing object.
- **`:ident` is accepted only in code position.** The compiler tracks quotes and comments and rejects any other
  placement ([`:ident`](api.md#ident)).
- **A `Template` is recognised by exact type.** Compilation accepts a `Template`, as its argument and as a nested
  value, only when `type(x) is Template`, never through `isinstance`. A nested value that is not exactly a `Template`
  is bound as a parameter, so an unsupported object fails in the encoder with `EncodeError`.

## Arrays, ranges and session encoding

- **Arrays and ranges are always sent in binary format.** There is no text literal for an element or a range bound
  ([Binary arrays and ranges](driver.md#binary-arrays-and-ranges)). Scalar parameter formats are unchanged. A scalar
  has no literal grammar, so a text-format scalar can corrupt only itself. `json.dumps` stays ASCII-only.
- **Session encoding watch.** tokio-postgres forces `client_encoding=UTF8` at startup but ignores a later
  `SET client_encoding` run by application SQL. When `client_encoding` leaves `UTF8`, or `standard_conforming_strings`
  turns `off`, the connection is tainted, every later call raises `InterfaceError`, and the pool discards it
  ([Session encoding watch](driver.md#session-encoding-watch)).
  - The taint is **sticky** for the life of the connection. `UTF8` and `UNICODE` (ASCII case-insensitive) both count
    as UTF-8. `standard_conforming_strings = off` set at role or database level taints the connection at connect.
  - This is a backstop against **data drift**. Binary arrays and ranges are what stop element injection. A statement
    already queued behind the `SET` can corrupt data but not change the structure of an array.
  - `DateStyle` and `IntervalStyle` are not watched, because every decoder rowx uses is binary.

## Connect options

- Option keys and values are validated ([Option validation](driver.md#option-validation)): a `server_settings` key
  **MUST** match the GUC grammar, an `extra` key **MUST** match `^[a-z_]+$`, and a NUL or other C0 control character
  in any value is an `InterfaceError` that names the key and never the value.
- Neither diagnostics nor exceptions may include a password. Redaction is by secret **value**, not by DSN syntax:
  every occurrence of the password, and of the value of any `extra` key containing `password` (such as
  `sslpassword`), is redacted in any exception message, `repr`, wrapped cause's `repr()` and checker diagnostic.
  Redaction replaces the decoded value and every raw spelling of it in the DSN text, and also masks URL userinfo and
  `password=` tokens syntactically, so a DSN that fails to parse is still covered. Empty secrets are skipped.
  `ConnectOptions.__repr__` shows `extra` values as `'<redacted>'`. Parameter values are redacted from Rust debug
  logs. Checker redaction is string processing over the URL the command already holds. It does not load a database
  backend to do it.

## TLS and authentication

- `sslmode` defaults to `prefer`, which is encrypted but not verified, as in libpq, sqlx and asyncpg. Under `prefer`
  or `disable` a MITM can impersonate the server.
- **Production recommendation:** `sslmode=verify-full&channel_binding=require`. `channel_binding=require` makes the
  client refuse any authentication method that cannot be bound to the TLS channel. `require` with `sslmode=disable`
  is a parse-time error. The server certificate must be one for which tokio-postgres-rustls can compute
  `tls-server-end-point` ([Connection and DSN](driver.md#connection-and-dsn)).
- `verify-full` and `require` never fall back to plaintext when the server answers `N` to SSLRequest, and a cancel
  request follows the connection's TLS mode.
- Non-goals: `require_auth` (tokio-postgres has no support), client certificates
  ([#304](https://github.com/mplemay/rowx/issues/304)), and changing the `sslmode` default.

## Server bytes

- The wire decoders parse bytes that a MITM controls under any `sslmode` weaker than `verify-full`. They **MUST**
  return `DecodeError` and never panic, because a panic surfaces in Python as `PanicException`, a `BaseException`.
  - `crates/rowx-postgres/src/wire/` denies `clippy::indexing_slicing` and `clippy::arithmetic_side_effects`, so slices
    use `get` and arithmetic is checked. The lints cover `wire/` only. The PyObject decoders in
    `packages/rowx-postgres/src/codec/` and the describe and explain parsing are covered by the panic-boundary test
    and the fuzz targets.
  - Every `wire/` decoder, array decoding for each element family, the EXPLAIN plan JSON reader and the DSN parser
    have a fuzz target, run with `-rss_limit_mb`. Encoders have `decode(encode(x)) == x` targets.
  - A malformed value of every type surfaces as `DecodeError` (the panic-boundary test).
  - The checker's own connection uses the same TLS settings, so its server bytes are untrusted below `verify-full`
    even though its database contents are trusted.
- A parameter or message of 2³¹ bytes or more raises `EncodeError` or `InterfaceError` and leaves the connection
  usable.

## Pool reset

- The default `RESET_QUERY` leaves a pooled connection with none of the previous checkout's session state: advisory
  locks, cursors, `LISTEN`, every `SET`, the role and session authorization, and temp tables
  ([Release](driver.md#release)).
  - The batch starts with `RESET SESSION AUTHORIZATION` (which `RESET ALL` does not cover) and then `RESET ALL`, and
    its function is `pg_catalog`-qualified.
  - When rowx believes no transaction is open, an idle guard runs with the reset. A custom reset query is a separate
    round trip after the guard or `ROLLBACK`. `reset_query=None` with no transaction flag sends no guard. A reset
    inside an open transaction fails and the connection is discarded.
  - A temp table matters because `pg_temp` is searched first for relations.
  - `DEALLOCATE ALL` is not part of the reset, because rowx's protocol-level statements are cached per connection.
    SQL-level `PREPARE` is unsupported across checkouts.
- `reset_query=None` turns the reset off, and is **unsafe for multi-tenant pools**.

## Checker

- The checker's guarantees and limits are in
  [Interface and checker session](nullability.md#interface-and-checker-session), including plan-time evaluation of
  functions and locking. With `--scratch` the checker executes migration files in a database it creates. Use a
  dedicated role on a CI or disposable database.
- No SQL text reaches the checker's EXPLAIN, whether the user's or a view definition. The checker prepares the
  statement and runs `EXPLAIN … EXECUTE "<name>"(NULL, …)` over its server-side name
  ([Algorithm](nullability.md#algorithm)).
  - A view definition takes the same path. It is prepared under a pinned `search_path` and explained by name.
  - Multi-statement SQL is still rejected by Parse.
  - `EXPLAIN … EXECUTE` evaluates its arguments, so a statement with a domain parameter is explained through its base
    types. No domain constraint is evaluated.
  - The plan pass accepts exactly one result set, and its JSON must have a `Plan` key.
- Server-supplied text (message, detail, hint) is sanitised before it reaches a terminal. Text output strips Unicode
  categories Cc (except `\n` and `\t`), Zl, Zp and Cf, and surrogate-escaped C1 bytes (U+DC80 through U+DC9F). U+200C
  (ZWNJ), U+200D (ZWJ) and U+00AD (soft hyphen) are kept ([Output](checker.md#output)).
- The checker never imports or executes user code, and the `template-construction` rule reads source only.

# Security and safety

## Threat model

| Trusted (authored by the developer or operator) | Untrusted |
|---|---|
| static t-string text, migration files, `reset_query`, the DSN and `ConnectOptions`, the checker's database ([plan-time evaluation](nullability.md#interface-and-checker-session)) | interpolated values, `{x:ident}` values, and the **types** of those values (`str` and `tuple` subclasses) |
| | server bytes when `sslmode` is weaker than `verify-full` (a MITM can impersonate the server) |
| | session state that application SQL can change (`SET client_encoding`, `SET ROLE`, temp tables) and that outlives a pool checkout |

Invariant: **no untrusted input can change the parse tree of a statement, or the element and bound structure of an
array or range parameter.**

Each guarantee below has a test. The sections that follow say how each row of the table is held.

## Parameters and SQL text

- Values are always bound as server-side parameters, never interpolated into SQL text.
  - `query` rejects every dynamic construct.
  - In `query_unchecked`, nested templates also bind their values, and `:ident` is quoted and length-checked
    ([`:ident`](api.md#ident-identifiers)).
  - There is no public raw-SQL escape.
- **`:ident` values are untrusted, and so are their types.** The compiler normalises a name once, with
  `str.encode`, and every check and the quoting then run on those bytes, so a `str` or `tuple` subclass that overrides
  `__contains__`, `__len__`, `encode`, `replace` or `__iter__` has no effect
  ([`:ident`](api.md#ident-identifiers)). Identifiers are **not** restricted to enum members: an application that takes
  a column name from a user **MUST** allowlist it (the guide shows `StrEnum`, and `Literal` with `assert_never`).
- **Compile limits** are `TypeError`s raised before any IO: a NUL in the SQL text, and more than 65535 parameters after
  flattening ([Compile limits](api.md#compile-limits)).
- **Hand-built templates.** The only way to put attacker-controlled text into the static part of a query is
  `Template(...)` with a non-literal string (or `t"…" + Template(user_str)`). The runtime compiles whatever `Template`
  it is given, and has no provenance check. The checker's `template-construction` rule
  ([Rules](checker.md#rules-all-errors)) rejects that construction in every file under the include roots, and also a
  non-literal format spec in any t-string literal there.
  - The rule is **best-effort**, and the docs say so. `getattr` and `eval` can still build a template, so can code
    outside the include roots, and so can generated code. It also has no ignore comments: the only escape is to
    exclude the path in config.
  - Decision: a runtime provenance check and an `assert_sql_safe` escape hatch were both rejected
    ([Security hardening](decision-log.md#security-hardening-2026-11-03)).
- **Migration files are trusted code.** `Migrator` and `rowx check --scratch` run their statements over the simple query
  protocol, because a file holds many statements. This is internal, and it adds no public raw-SQL escape. Only the
  table name is built into SQL by rowx, and it is parsed into quoted identifiers
  ([History table](migrations.md#history-table)); so are database names in `rowx database` and `--scratch`.
- **Internal SQL names `pg_catalog` objects explicitly** (`pg_catalog.now()` in the history DDL,
  `pg_catalog.pg_advisory_unlock_all()` in `RESET_QUERY`, and so on). Operators are qualified as
  `OPERATOR(pg_catalog.…)` too, because operators resolve through `search_path` like functions. So a role or DSN
  `search_path` can't make rowx's own statements resolve to a shadowing object
  ([History table](migrations.md#history-table)). A test runs migrations under `search_path = evil, pg_catalog`, and a
  test asserts that rowx's internal SQL constants, `RESET_QUERY` included, call no unqualified function.
- **`:ident` is accepted only in code position;** the compiler enforces it
  ([`:ident`](api.md#ident-identifiers)).
- **A `Template` is recognised by exact type.** `compile_template` accepts a `Template`, as its argument and as a
  nested value, only when `type(x) is Template` (`Template` can't be subclassed), never through `isinstance`, so only an
  exact `Template` is compiled. A nested value that isn't exactly a `Template` is
  bound as a parameter, so an unsupported object fails in the encoder with `EncodeError`
  ([t-string compilation](api.md#template-recognition)).

## Arrays, ranges and session encoding

- **Arrays and ranges are always sent in binary format.** There is no text literal for an element or a range bound,
  so there is no escaping rule to get wrong ([Binary arrays and ranges](driver.md#binary-arrays-and-ranges)). A test
  asserts that no array or range parameter is ever sent in text format.
- Scalar parameter formats are unchanged. A scalar has no literal grammar, so a text-format scalar can corrupt only
  itself. `json.dumps` stays ASCII-only, which a test pins.
- **Session encoding watch.** tokio-postgres forces `client_encoding=UTF8` at startup but ignores a later
  `SET client_encoding` run by application SQL. rowx watches `ParameterStatus`: when `client_encoding` leaves `UTF8`,
  or `standard_conforming_strings` turns `off`, the connection is tainted, every later call raises `InterfaceError`,
  and the pool discards it ([Session encoding watch](driver.md#session-encoding-watch)).
  - The taint is **sticky** for the life of the connection: a later `RESET ALL` or `SET client_encoding = UTF8` doesn't
    clear it. `standard_conforming_strings = off` set at role or database level taints the connection at connect, so
    its first call raises `InterfaceError`.
  - This is a backstop against **data drift** (text stored in the wrong encoding). It is not what stops element
    injection: binary arrays and ranges do. So the watch's residual window, a statement already queued behind the
    `SET`, can corrupt data but not change the structure of an array.
  - A legitimate `SET standard_conforming_strings = off` therefore costs a connection. `DateStyle` and `IntervalStyle`
    are not watched, because every decoder rowx uses is binary.

## Connect options

- Option keys and values are validated ([Option validation](driver.md#option-validation)): a `server_settings` key
  **MUST** match the GUC grammar, an `extra` key **MUST** match `^[a-z_]+$`, and a NUL or other C0 control character in
  any value is an `InterfaceError` that names the key and never the value. Without this, a tab or newline in a key
  would be split by the server into extra startup `-c` options.
- Neither diagnostics nor exceptions may include a password. Redaction is by secret **value**, not by DSN syntax: every
  occurrence of the password, and of the value of any `extra` key containing `password` (such as `sslpassword`), is
  redacted in any exception message, `repr`, wrapped cause's `repr()` and checker diagnostic, whichever DSN form carried
  it (URL userinfo, `?password=`, or key=value `password=`). Redaction replaces the decoded value and every raw spelling
  of it in the DSN text (percent-encoded, escaped), and also masks URL userinfo and `password=` tokens syntactically, so
  a DSN that fails to parse is still covered; empty secrets are skipped. `ConnectOptions.__repr__` shows `extra` values
  as `'<redacted>'` ([API](api.md#rowxpostgres)). Parameter values are redacted from Rust debug logs
  ([Codec](driver.md#codec-rowx-postgres-pysrccodec-rowx-postgressrcwire)).

## TLS and authentication

- `sslmode` defaults to `prefer`, which is encrypted but not verified, as in libpq, sqlx and asyncpg. Under `prefer`
  or `disable` a MITM can impersonate the server, so it can request cleartext or MD5 authentication and receive the
  password.
- **Production recommendation:** `sslmode=verify-full&channel_binding=require`. `channel_binding=require` makes the
  client refuse any authentication method that cannot be bound to the TLS channel, so a server that asks for a
  cleartext or MD5 password, or SCRAM without `-PLUS`, gets nothing before the connection fails. rowx has a typed
  `ConnectOptions.channel_binding` option, and `require` with `sslmode=disable` is a parse-time error. The server
  certificate must be one for which tokio-postgres-rustls can compute `tls-server-end-point` (see the note in
  [Connection and DSN](driver.md#connection-and-dsn-rowx-postgressrcdsnrs-tlsrs-connrs))
  ([Connection and DSN](driver.md#connection-and-dsn-rowx-postgressrcdsnrs-tlsrs-connrs)).
- `verify-full` and `require` never fall back to plaintext when the server answers `N` to SSLRequest, and a cancel
  request follows the connection's TLS mode. Fake-server tests cover both.
- Non-goals: `require_auth` (tokio-postgres has no support), client certificates (roadmap item 8), and changing the
  `sslmode` default.

## Server bytes

- The wire decoders parse bytes that a MITM controls under any `sslmode` weaker than `verify-full`. They **MUST**
  return `DecodeError` and never panic, because a panic surfaces in Python as `PanicException`, a `BaseException`.
  - `wire/` denies `clippy::indexing_slicing` and `clippy::arithmetic_side_effects` at the module level, so slices
    use `get` and arithmetic is checked. The lints cover `wire/` only: the PyObject decoders in
    `crates/rowx-postgres-py/src/codec/` and the describe and explain parsing are covered by the panic-boundary test and
    the fuzz targets instead.
  - Every `wire/` decoder, array decoding for each element family, the EXPLAIN plan JSON reader and the DSN parser have
    a fuzz target, run with `-rss_limit_mb` so an over-allocation fails. Encoders have
    `decode(encode(x)) == x` targets ([Testing](testing.md)).
  - A malformed value of every type surfaces as `DecodeError` (the panic-boundary test). It decodes through Python, so
    it exercises `codec/` as well as `wire/`.
  - The checker's own connection uses the same TLS settings, so its server bytes are untrusted below `verify-full` even
    though its database contents are trusted.
- Length fields are bounds-checked on the way out as well. A parameter or message of 2³¹ bytes or more raises
  `EncodeError`/`InterfaceError` and leaves the connection usable (the RUSTSEC-2024-0363 class, which tokio-postgres
  fixed by checking `i32::from_usize` and `u16::from_usize`). A regression test covers it.

## Pool reset

- The default `RESET_QUERY` leaves a pooled connection with none of the previous checkout's session state: advisory
  locks, cursors, `LISTEN`, every `SET`, the role and session authorization, and temp tables
  ([Release](driver.md#release)). Tests assert that a role, a session authorization, `search_path`, a temp table and an
  advisory lock do not survive a release.
  - The batch runs under the previous checkout's session state, so it starts with `RESET SESSION AUTHORIZATION` (which
    `RESET ALL` does not cover) and then `RESET ALL`, and its function is `pg_catalog`-qualified
    ([Release](driver.md#release)).
  - The reset batch starts with an idle guard when rowx believes no transaction is open, so a reset inside an open
    transaction fails and the connection is discarded ([Release](driver.md#release)).
  - A temp table matters because `pg_temp` is searched first for relations, so a leftover one would shadow an
    application table for the next checkout.
  - `DEALLOCATE ALL` is not part of the reset, because rowx's protocol-level statements are cached per connection.
    SQL-level `PREPARE` is unsupported across checkouts.
- `reset_query=None` turns the reset off, and is **unsafe for multi-tenant pools**. The guide says so.

## Checker

- The checker's guarantees and limits are in
  [Interface and checker session](nullability.md#interface-and-checker-session), including plan-time evaluation of
  functions and locking, and the amended statement for `--scratch`, which executes migration files in a database it
  creates. The docs recommend a dedicated role on a CI or disposable database.
- No SQL text reaches the checker's EXPLAIN, whether the user's or a view definition. The checker prepares the
  statement and runs `EXPLAIN … EXECUTE "<name>"(NULL, …)` over its server-side name, so the statement boundary does not
  depend on Parse alone ([Algorithm](nullability.md#algorithm), step 3, and
  [Plan pass by statement name](nullability.md#plan-pass-by-statement-name)).
  - A view definition takes the same path. It is prepared under a pinned `search_path` and explained by name, never
    concatenated ([View columns](nullability.md#view-columns)).
  - Multi-statement SQL is still rejected by Parse, and a test asserts that it never runs.
  - `EXPLAIN … EXECUTE` evaluates its arguments, so a statement with a domain parameter is explained through its base
    types: the same SQL is prepared again with each domain unwrapped. No domain constraint is evaluated, so a `CHECK`
    that calls a `VOLATILE` function or takes a lock does not run.
  - The plan pass accepts exactly one result set, and its JSON must have a `Plan` key.
  - Tests assert that no EXPLAIN text contains the user's SQL or a view definition, and that a domain `CHECK` never runs
    during describe.
- Server-supplied text (message, detail, hint) is sanitised before it reaches a terminal: text output strips C0 and C1
  control characters except `\n` and `\t` ([Output](checker.md#output)).
- The checker never imports or executes user code, and the `template-construction` rule reads source only.

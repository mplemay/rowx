# Driver

PostgreSQL connections, execution, codecs and pooling. Open them from `rowx.postgres`. The `rowx-postgres`
distribution imports the same objects from `rowx_postgres`. The query methods that run on an executor are in
[API](api.md).

The native module is `rowx_postgres._core`. Pure Rust lives in `crates/rowx-postgres/src/` (`dsn`, `tls`, `conn`,
`stmt_cache`, `exec`, `cancel`, `pool`, `wire/`) and in `crates/rowx-core/src/` (`state.rs` for claim and settle,
`cancel.rs` for cancellation plumbing, `row_shape.rs` for column-to-field mapping). The PyObject codecs and bindings
live under `packages/rowx-postgres/src/`. `crates/rowx-postgres` and `crates/rowx-core` do not use PyO3.

## Connection and DSN

```python
from rowx.postgres import ConnectOptions, Pool, connect

async with await connect("postgresql://app@localhost/app") as conn:
    ...

async with Pool("postgresql://app@localhost/app", max_size=20) as pool:
    ...
```

`connect` and `Pool` take `str | ConnectOptions`. `ConnectOptions.from_dsn` uses the parser in
`crates/rowx-postgres/src/dsn.rs`. `password` is omitted from `repr`. `extra` values render as `'<redacted>'`.
Keyword DSNs (`host=…`) are accepted. `options=-c search_path=…` round-trips into `server_settings` and is sent as
startup options, so it survives `RESET ALL`.

`sslmode` defaults to `prefer`, which encrypts and does not verify the server. `verify-full` checks the certificate
and the host. `channel_binding=require` refuses an authentication method that cannot be bound to the TLS channel,
and it is an error together with `sslmode=disable`. `verify-full` and `require` do not fall back to plaintext when
the server refuses SSL. A cancel request uses the same TLS mode as the connection. The TLS stack is rustls with the
ring provider (`crates/rowx-postgres/src/tls.rs`).

For a verified connection use `sslmode=verify-full` and `channel_binding=require`. Client certificates are
[#304](https://github.com/mplemay/rowx/issues/304).

## Option validation

Keys and values are checked before any socket is opened. A `server_settings` key must match the GUC grammar. An
`extra` key must match `^[a-z_]+$`. A NUL or other C0 control in a value is `InterfaceError`, and the message names
the key and not the value. `statement_cache_size` defaults to 256. `0` disables the cache.

No exception message, `repr` or checker diagnostic may contain a password. Redaction is by secret value and also
masks URL userinfo and `password=` tokens, so a DSN that fails to parse is still covered. It does not load a
database backend to do it. See [Connect options](security.md#connect-options).

## Execution

One operation runs on a connection at a time. A second concurrent call raises `ConnectionBusyError`. Pool checkout
is the exception: the pool waits, and a timeout is `PoolTimeoutError`.

Each call compiles SQL with that executor's compiler
(`packages/rowx-postgres/src/rowx_postgres/_compiler.py`). Capturing the t-string does not. The shape check (arity,
and for a named row the column-to-field mapping from `crates/rowx-core/src/row_shape.rs`) runs against the prepared
statement before execution, including when the result is empty.

A stale cached plan (`0A000` from `RevalidateCachedQuery`) is prepared again once. A vanished statement (`26000`)
clears the cache and runs once more. There is no second retry. Inside a transaction a failure that aborts the
transaction (`25P02`) is not retried. The error hint is `schema changed inside a transaction; ROLLBACK and retry`.

`fetch` claims the connection on the first `__anext__`. Streaming, early exit and cancellation of a stream are in
[Streaming](api.md#streaming).

## Codecs

Wire codecs in `crates/rowx-postgres/src/wire/` are pure functions over bytes. The PyObject codecs in
`packages/rowx-postgres/src/codec/` are the boundary. Dispatch uses the `tokio_postgres` type from the statement, not
the bare OID, so a domain uses its base codec. The type rules are [Type mapping](type-mapping.md).

A value that does not decode is `DecodeError`. A value that does not encode is `EncodeError`. The connection stays
usable. A parameter or message of 2³¹ bytes or more is the same: an error, not a broken connection.

## Binary arrays and ranges

Arrays and ranges are always sent in binary format. There is no text literal for an element or a range bound, so
untrusted element text cannot change the array's shape. Scalar parameters may still use text format. A scalar has
no element grammar, so a text-format scalar can corrupt only itself. `json.dumps` stays ASCII-only.

Nested lists must be rectangular. The encoder takes each dimension's length from the first element at that depth
and raises `EncodeError` (`the nested lists are ragged`) when another element has a different length or a
different depth. A NULL element is still binary: the wire length is `-1` and there is no payload. An element is
never sent as text. A list deeper than six levels is `EncodeError`. An empty list at any level, once the shape is
rectangular, encodes as an empty array.

## Session encoding watch

tokio-postgres sets `client_encoding=UTF8` at startup and does not notice a later `SET`. When `client_encoding`
leaves UTF-8, or `standard_conforming_strings` turns `off`, the connection is tainted. Every later call raises
`InterfaceError`, and a pool discards the connection. `UTF8` and `UNICODE` both count as UTF-8. A role or database
default of `standard_conforming_strings=off` taints the connection at connect. The taint lasts for the life of the
connection. `DateStyle` and `IntervalStyle` are not watched. Every decoder rowx uses for a value it cares about is
binary.

## Statement cache

Each connection keeps an LRU of prepared statements (`crates/rowx-postgres/src/stmt_cache.rs`), sized by
`statement_cache_size`. The checker's own connection sets the size to 0.

Behind a transaction-mode pooler, a prepared statement can land on a different server connection than the one that
prepared it. Connect with `statement_cache_size=0`, or use a pooler that tracks prepared statements. Session-mode
pooling keeps one server connection per client connection, and the default cache is safe there. The migrator also
needs a direct or session-mode connection, because its lock is session-level.

## Cancellation

Cancelling the task that is awaiting a query moves the connection from busy to settling
(`crates/rowx-core/src/state.rs`). The abandoned operation drains to `ReadyForQuery`, or to an error, before the
next operation starts. A cancel that arrives between operations does not sit on the connection and spoil a later
query. The Postgres cancel request is `crates/rowx-postgres/src/cancel.rs`, on the shared plumbing in
`crates/rowx-core/src/cancel.rs`. The request uses the connection's TLS settings. Leaving a `fetch` loop early does
not cancel. It drains. See [Streaming](api.md#streaming).

## Pool

`rowx.postgres.Pool` does no IO in the constructor. `async with` calls `open` and `close`. Defaults are
`min_size=0`, `max_size=10`, `acquire_timeout=30`, `idle_timeout=600`, `max_lifetime=1800`, `max_queries=None` and
`ping_after=30`. `min_size` must sit between 0 and `max_size`. A closed pool raises `InterfaceError`.

`acquire` waits up to `acquire_timeout` and raises `PoolTimeoutError`. The connection is released when the block
ends. A query method on the pool checks a connection out for that call only.

Idle connections older than `idle_timeout`, or older than `max_lifetime`, are closed instead of reused. `None`
disables that limit. After `ping_after` seconds idle, a connection is checked before use. `max_queries` closes a
connection after that many successful checkouts.

The pool core is `crates/rowx-core/src/pool/`. The Postgres connector is `crates/rowx-postgres/src/pool.rs`. The
Python constructor is `packages/rowx-postgres/src/rowx_postgres/_pool.py`, and the binding is
`packages/rowx-postgres/src/pool.rs`.

### Release

On release the pool runs `reset_query`, which defaults to `RESET_QUERY`:

```text
RESET SESSION AUTHORIZATION; RESET ALL; SELECT pg_catalog.pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; DISCARD TEMP;
```

That drops the previous checkout's advisory locks, cursors, `LISTEN` subscriptions, `SET` values, role, session
authorization and temporary tables. The batch starts with `RESET SESSION AUTHORIZATION`, which `RESET ALL` does not
cover, and the unlock is schema-qualified. When rowx believes no transaction is open, an idle guard runs with the
reset. A custom `reset_query` is a separate round trip. `reset_query=None` sends no reset, and is unsafe when
untrusted SQL can run on a connection that will be reused. A reset that fails discards the connection.
`DEALLOCATE ALL` is not part of the reset. The statement cache is per connection and stays.

`init` runs once, when the connection is first established. `setup` runs after checkout and before the caller sees
the connection. `reset` runs before the connection returns to the idle set. A hook that raises discards the
connection.

## Transactions

`conn.transaction()` starts a transaction and returns an executor. Nested `transaction()` on that object is a
savepoint. A savepoint rejects `isolation`, `readonly` and `deferrable`. Those arguments belong to the outer
transaction only. Committing or rolling back the outer transaction ends the savepoints. A `Transaction` is not a
valid target for `Migrator`. Use the connection or the pool.

The binding is `packages/rowx-postgres/src/transaction.rs`. The Python type is
`packages/rowx-postgres/src/rowx_postgres/_transaction.py`.

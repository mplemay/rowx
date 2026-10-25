# Rust driver

## Connection and DSN (`rowx-pg/src/dsn.rs`, `tls.rs`, `conn.rs`)

rowx **pre-parses** the DSN, in both URL and key=value forms. It removes `sslmode`,
`sslrootcert`, `sslcert`, `sslkey` and `sslcrl`, and passes everything else to
`tokio_postgres::Config`. This is needed because tokio-postgres rejects `verify-ca`,
`verify-full` and `sslrootcert`.

| `sslmode` | tokio-postgres mode | rustls verifier |
|---|---|---|
| `disable` | `Disable` | – |
| `prefer` (default) | `Prefer` | accepts any cert, as libpq does |
| `require` | `Require` | accepts any cert, but verifies the CA chain when `sslrootcert` is given (libpq) |
| `verify-ca` | `Require` | chain verified against `sslrootcert`, else webpki roots; hostname not checked |
| `verify-full` | `Require` | full WebPKI verification, including the hostname |

- `sslcert`/`sslkey` (client certs) and `sslcrl` raise `InterfaceError("not supported in
  v1")`.
- Each connection spawns its tokio-postgres connection task through the runtime `Handle`
  stored at module init. When that task fails, the state becomes `Broken`.
- **Error mapping.** DSN parse errors, IO errors, TLS errors and timeouts become
  `InterfaceError`. A server `ErrorResponse` becomes `DatabaseError`.
- `close()` works as follows:
  - If the state is `Busy` or `Settling`, it cancels and settles first ([Cancellation](#cancellation-cancelrs)).
  - It then drops the client and awaits the connection task. Close is idempotent.

## Execution path (`exec.rs`)

The binding signatures (in `_core.pyi`) are:

```text
Connection._fetch(sql: str, params: list[object], mask: list[bool] | None,
                  scalar: bool, limit_one: bool, arity: int | None) -> list[object]
Connection._execute(sql: str, params: list[object]) -> int     # Python wraps it in QueryResult
Connection._stream(sql: str, params: list[object], mask: list[bool] | None,
                   scalar: bool, arity: int | None, batch_size: int) -> RowStream   # sync
RowStream._next() -> list[object]      # awaitable; [] means exhausted
RowStream.close() -> None              # sync, idempotent
RowStream.rows_affected -> int | None  # set once exhausted
```

1. **Under the GIL**, synchronously in the `#[pymethods]` body:
   - claim the connection, Idle → Busy ([Connection state machine](#connection-state-machine-staters));
   - extract every parameter into the neutral `PyParam` enum ([Codec](#codec-rowx-pysrccodec-rowx-pgsrcwire)).
2. **Without the GIL**, in a spawned tokio task:
   - Look up the statement cache; on a miss, `prepare(sql)` with no parameter types, so
     the server infers them.
   - Check that `params.len()` equals `stmt.params().len()`, else raise `InterfaceError`.
   - Check that `arity` equals `stmt.columns().len()`, else raise `RowShapeError`.
   - Encode each parameter against its `Type` ([Codec](#codec-rowx-pysrccodec-rowx-pgsrcwire)).
3. Run `query_raw`. Per-parameter formats come from each value's
   `ToSql::encode_format(&Type)`, and tokio-postgres always requests binary results.
4. Collect the raw rows, `Vec<Option<Bytes>>` with types. Apply the `mask` NULL check while
   collecting. With `limit_one`, drop the stream after the first row.
5. **With the GIL**, in one pass, build Python tuples (or bare values when `scalar`).
6. Move the state Busy → Idle **before** the result is delivered to Python, so
   `await a(); await b()` never sees Busy.

`exec.rs` in `rowx-pg` is split into two parts:

- **`open()`**: prepare through the cache, the shape check (including the re-prepare of a
  stale cached statement), encode, then `query_raw`. The retrier wraps it.
- **Consumers**: the collector behind `_fetch` (all rows, or the first with `limit_one`),
  and the streaming consumer ([Streaming](#streaming-execrs)).

**Schema-change retry.** This applies when the server answers `0A000` and
`DbError::routine() == "RevalidateCachedQuery"` ("cached plan must not change result type").

- Evict the cache entry, re-prepare and retry **once**.
- If the retry fails with `25P02` (inside a transaction), raise the **original** error as
  `DatabaseError`, with the hint "schema changed inside a transaction; ROLLBACK and retry".
- On `26000` ("prepared statement does not exist", e.g. after `DISCARD ALL`), clear the
  whole cache and retry once.

## Streaming (`exec.rs`)

This implements [Streaming](api.md#streaming-fetch).

1. **Claim.** Python calls `_stream` from the first `RowStream.__anext__`. Under the GIL, it
   claims the connection and extracts parameters, as in step 1 of the execution path.
2. **Producer task.**
   - `_stream` spawns a producer task. The task owns the connection lock and the
     tokio-postgres `RowStream`.
   - The claim and the guard live in a slot shared by the binding object and the producer.
     Exactly one party takes the claim out of the slot:
     - `close()` or Drop releases it (Busy → Idle);
     - the producer releases it at `End` or an error, before sending that message;
     - the canceller hands it off (Busy → Settling).
   - It runs `open()`, then sends `Batch(Vec<Row>)` messages, and finally
     `End { rows_affected }` (from `RowStream::rows_affected()`) or an error, over a
     `tokio::sync::mpsc::channel(1)`.
3. **Batches.**
   - A batch waits for one row, then takes the rows that are ready without waiting, up to
     `batch_size`.
   - The `mask` NULL check runs per row. On a violation, the rows before it go out as a
     batch, then the error.
4. **`_next()`** awaits one message without the GIL, then decodes it with the GIL.
   - Rows are decoded one at a time. If row k fails (for example, a `DecodeError` for
     `infinity`), `_next()` returns rows 0..k and keeps the error.
   - The next `_next()` raises the kept error. If k is 0, the error is raised at once.
   - A raised error ends the stream: the binding releases the stream as in early exit
     (step 5), and later calls return `[]`.
   - After `End`, `_next()` records `rows_affected` and returns `[]`.
   - `_fetch` keeps its single-pass builder, since it has no prefix to preserve.
5. **Early exit.** `close()`, or Drop of the binding object, does the following
   **synchronously**:
   - It disarms the guard and releases the claim from the shared slot (Busy → Idle), so the
     connection is Idle before `aclose()` returns.
   - It closes the receiver. The producer waits for rows with `select!` on the stream and
     on `Sender::closed()`, so it sees the closure even while it is waiting on the server.
     It then drops the tokio-postgres `RowStream`, so the connection task discards the
     remaining rows, and releases the connection lock.
   - The next operation claims the Idle connection and waits briefly on the lock, never
     seeing Busy. It is sent after the stream is dropped, so it queues on the wire behind
     the remaining rows.
   - Drop does no async work, so it is safe outside a runtime.
6. **Cancellation.**
   - The guard ([Cancellation](#cancellation-cancelrs)) is armed from the request until
     `End` or an error.
   - Each `_next()` future registers a `Hook` via the done-callback watcher. Cancelling
     that future while the guard is armed fires the cancel path for the whole stream.
   - Unlike the other operations, dropping the receiver is **not** a cancellation.
7. **Backpressure.**
   - The producer blocks on `send` until Python takes the batch. tokio-postgres's
     per-request response channel is also bounded, so its connection task stops reading
     the socket, and TCP pushes back on the server.
   - Rows in flight are bounded by one batch in the channel, one being built, and socket
     buffers.

**Acceptance:**

- A full stream yields every row in order. `result` is `None` mid-stream and
  `QueryResult(rows_affected=N)` once exhausted, including N for `INSERT … RETURNING`.
- After `break` (with no other reference to the stream), `aclose()`, leaving `async with`,
  or garbage collection, the next query on the same connection succeeds, and `result`
  stays `None`. An early-exited `INSERT … RETURNING` commits every row.
- `aclose()` while the producer is waiting on the server (`SELECT 1 UNION ALL SELECT
  pg_sleep(5)`, closed after the first row) returns with the connection Idle. The next
  query runs without `ConnectionBusyError`.
- Another operation while a stream is open raises `ConnectionBusyError`.
- A mid-stream NULL violation yields the rows before it, then raises `RowShapeError`. The
  next query succeeds.
- A batch whose third row holds `'infinity'::date` yields the two rows before it, then
  raises `DecodeError`. The next query succeeds.
- Cancelling a task awaiting `__anext__` on `pg_sleep` frees the backend
  (`pg_stat_activity` within 2 s), and the next query succeeds.
- In a 300-iteration stress test, a stream is cancelled at a random point and a query
  follows immediately. **Zero** follow-ups may fail.
- `conn.close()` mid-stream works, and the next `__anext__` raises `InterfaceError`.
- With `batch_size=1`, every trip yields exactly one row. With the default, a 10k-row
  result needs at most ⌈10000 / 1024⌉ trips once all rows are buffered.
- Benchmark (non-gating): `fetch()` over the 100k-row benchmark is within 1.25× of `all()`.

## Codec (`rowx-py/src/codec/`, `rowx-pg/src/wire/`)

**Extract (GIL held).** Python values are converted to a `Send` enum:

```rust
enum PyParam { Null, Bool(bool), Int(i64), BigInt(String), Float(f64), Str(String),
  Bytes(Vec<u8>), Decimal(String), Date{..}, Time{.., utcoffset: Option<i32>},
  DateTime{.., utcoffset: Option<i32>}, Timedelta{days,secs,micros}, Uuid([u8;16]),
  Seq(Vec<PyParam>), Json(String) /* json.dumps result, produced lazily: see below */,
  Other(Py<PyAny>) }
```

`dict`, and any value that is not one of the above, becomes `Other`.

**Encode (no GIL).** Each value is encoded against its parameter `Type`, using the
`accepted_params` and `encode_format` from [Type mapping](type-mapping.md#type-mapping-single-source-of-truth).

- A type not in `accepted_params` raises `EncodeError`, naming `$n`, the Python type and the
  Postgres type.
- When the target is `json`/`jsonb`, the encoder reacquires the GIL (`Python::attach`) to
  run `json.dumps` on the original object.

**Array text literal** (used when an element needs text format):

- Build it recursively: `{` elements joined by `,` `}`.
- Every non-NULL element is double-quoted, with `\` and `"` backslash-escaped. NULL is the
  bare `NULL`, and an empty list is `{}`.
- Nested lists must be rectangular; a ragged list raises `EncodeError`.
- This is still a bound parameter, so an escaping bug can corrupt values but can never
  inject SQL.

**Decode.** A custom `FromSql`, `Raw<'a>(Option<&'a [u8]>, &Type)`, accepts every type. Pure
wire parsers in `rowx-pg/src/wire/` produce Rust values, and `rowx-py/src/codec/` turns them
into Python objects.

- Constructors for `Decimal`, `UUID`, `datetime`, `date`, `time`, `timedelta` and
  `timezone.utc` are cached.
- jsonb strips its version byte, then calls `json.loads`.

**Logging.** tokio-postgres's `log` debug output prints parameters through `Debug`. The
parameter wrapper's `Debug` impl **MUST** redact values.

**Performance bar (v1).** Fetching 100k rows of (int4, text, timestamptz) with a release
build **should** take no longer than asyncpg does on the same machine. The spike measured
35.7 ms against asyncpg's 46.5 ms. This is tracked by a non-gating benchmark job.

## Statement cache (`stmt_cache.rs`)

- An LRU per connection, keyed by SQL string, with capacity `statement_cache_size`
  (default 256). Setting it to `0` disables caching: every call uses an unnamed prepare.
- A `Statement` is `Clone`, backed by an `Arc`. tokio-postgres sends Close only when the
  **last** clone drops, so evicting an entry while a query using it is in flight is safe.
- A future cancelled during `prepare` may leak a named server statement until the
  connection closes. This is documented and accepted.
- **External poolers.** In transaction mode, users need `statement_cache_size=0`, or
  PgBouncer ≥ 1.21 with `max_prepared_statements`. This is documented.

## Connection state machine (`state.rs`)

The states are `Idle | Busy | Settling | Broken | Closed`, held in an atomic and changed by
compare-and-swap in the synchronous part of each `#[pymethods]` call.

| Call arrives while | Result |
|---|---|
| `Idle` | → `Busy`, proceed |
| `Busy` | raise `ConnectionBusyError` immediately (asyncpg semantics; no queueing) |
| `Settling` | await the settle notification, then retry the claim |
| `Broken` / `Closed` | raise `InterfaceError` |

Decision: there is one operation per connection. Transactions are written as
`query(t"BEGIN").execute(conn)` and `COMMIT`/`ROLLBACK`, and the user **MUST** own the
connection from a single task. The docs say this.

## Cancellation (`cancel.rs`)

1. Each operation runs as a spawned tokio task that owns the request: the `Statement` and
   the `RowStream`. The Python-facing future awaits a oneshot from that task.
2. A **guard** is armed from "request sent" to "response fully consumed", and is disarmed
   on **every** completion path, success or error. Only dropping the Python-facing future
   while the guard is armed counts as a cancellation. Error paths never send cancels.
3. **On cancellation**, the state becomes `Settling`, and a detached task, spawned with the
   stored `Handle` because `Drop` may run outside a runtime, does the following:
   - (a) It opens a connection to the server, using the same TLS settings. It writes the
     16-byte CancelRequest (from the backend key data), then **reads until EOF**. The
     server closes this socket only after it has signalled the backend.
     tokio-postgres's `cancel_query` doesn't wait for this, so rowx implements it itself.
   - (b) It keeps draining the abandoned operation until `ReadyForQuery` or an error.
   - (c) It moves the state to `Idle`, or to `Broken` on an IO error, and notifies waiters.

Without (a) and (b), a stale cancel kills the *next* query. The review measured 166 of 300
follow-up queries failing with 57014.

A cancelled statement inside `BEGIN` aborts the transaction, which then gives 25P02. The user
must `ROLLBACK`; this is documented.

**Acceptance:**

- Cancelling `pg_sleep(3)` frees the backend. A poll of `pg_stat_activity` on another
  connection shows the sleep gone within 2 s.
- The next query on the same connection succeeds.
- In a 300-iteration stress test, a ~5 ms query is cancelled at a random point and a query
  follows immediately. **Zero** follow-ups may fail.

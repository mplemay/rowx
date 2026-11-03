# Rust driver

## Connection and DSN (`rowx-postgres/src/dsn.rs`, `tls.rs`, `conn.rs`)

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
- **`ConnectOptions`** ([API](api.md#rowxpostgres)) is converted to the same pre-parsed configuration. `server_settings`
  are sent inside the startup `options` as `-c k=v`, with libpq escaping, because tokio-postgres puts only `user`,
  `database`, `application_name`, `client_encoding` and `options` in the startup packet. The DSN parser parses the `-c`
  pairs of `options` back into `server_settings` (overrides win), and every other key it keeps (keepalives,
  `target_session_attrs`, multiple hosts) is carried in `extra` and passed through.
- `connect_timeout` bounds each connect attempt.
- `ping()` sends an empty simple query. `reset()` runs `RESET_QUERY` ([Release](#release)).
- `close()` works as follows:
  - If the state is `Busy` or `Settling`, it cancels and settles first ([Cancellation](#cancellation-cancelrs)).
  - It then drops the client and awaits the connection task. Close is idempotent.

## Execution path (`exec.rs`)

The binding signatures (in `_core/postgres.pyi`) are:

```text
Connection._fetch(sql: str, params: list[object], mask: list[bool] | None,
                  scalar: bool, limit_one: bool, arity: int | None,
                  shape: RowShape | None) -> list[object]
Connection._execute(sql: str, params: list[object]) -> int     # Python wraps it in QueryResult
Connection._stream(sql: str, params: list[object], mask: list[bool] | None,
                   scalar: bool, arity: int | None, batch_size: int,
                   shape: RowShape | None) -> RowStream   # sync
RowStream._next() -> list[object]      # awaitable; [] means exhausted
RowStream.close() -> None              # sync, idempotent
RowStream.rows_affected -> int | None  # set once exhausted
map_columns(columns: list[str], fields: list[tuple[tuple[str, ...] | None, bool]],
            positional: bool) -> tuple[list[int | None], list[ShapeIssue]]
                                       # module function, for the checker (Named rows)
```

`shape` is the trailing argument and the existing arguments keep their meaning. It is `None` for a tuple or scalar plan.
For a named plan, Python passes `mask=None`, `scalar=False`, `arity=None` and the `RowShape`; the binding derives the
rest ([Named rows](#named-rows)). `Pool` and `Transaction` take the same arguments.

`_stream` returns a handle whose first `_next()` claims a connection. On a `Connection` or `Transaction` that is a
synchronous claim, so `ConnectionBusyError` is raised from the first `__anext__`. On a `Pool` it is an async acquire
(`PoolTimeoutError` can be raised there). `Pool._fetch` and `Pool._execute` acquire, run, deliver and release in one
Rust future ([Fast path](#fast-path)). The shared Python never imports these classes: it calls them through the
`Executor` protocol ([API](api.md#rowx-rowx__init__py)).

1. **Under the GIL**, synchronously in the `#[pymethods]` body:
   - claim the connection, Idle → Busy ([Connection state machine](#connection-state-machine-staters));
   - extract every parameter into the neutral `PyParam` enum
     ([Codec](#codec-rowx-postgres-pysrccodec-rowx-postgressrcwire)).
2. **Without the GIL**, in a spawned tokio task:
   - Look up the statement cache; on a miss, `prepare(sql)` with no parameter types, so
     the server infers them.
   - Check that `params.len()` equals `stmt.params().len()`, else raise `InterfaceError`.
   - Check that `arity` equals `stmt.columns().len()`, else raise `RowShapeError`. For a named plan, map the columns to
     the fields instead ([Named rows](#named-rows)).
   - Encode each parameter against its `Type` ([Codec](#codec-rowx-postgres-pysrccodec-rowx-postgressrcwire)).
3. Run `query_raw`. Per-parameter formats come from each value's
   `ToSql::encode_format(&Type)`, and tokio-postgres always requests binary results.
4. Collect the raw rows, `Vec<Option<Bytes>>` with types. Apply the `mask` NULL check while
   collecting. With `limit_one`, drop the stream after the first row.
5. **With the GIL**, in one pass, build Python tuples (or bare values when `scalar`, or named rows,
   [Named rows](#named-rows)).
6. Move the state Busy → Idle **before** the result is delivered to Python, so
   `await a(); await b()` never sees Busy.

`exec.rs` in `rowx-postgres` is split into two parts:

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

## Named rows

This implements [Named row types](api.md#named-row-types). The mapping is pure and lives in `rowx-core`, the check runs
in `rowx-postgres`, and the construction runs in `rowx-postgres-py`.

**`rowx-core/src/row_shape.rs`** has no pyo3 dependency and has `cargo` tests.

- `column_name(raw: &str) -> (&str, Option<Override>)` strips a single trailing `!` or `?`, and exempts `?column?`.
  `describe.rs` (`apply_overrides`) calls it, so the stripping has one implementation.
- The specs are `FieldSpec { names: Names, required: bool }`, where `Names` is `Known(Vec<String>)` or `Unknown`, and
  `Matching::{ByName, Positional}`.
- `map_columns(raw_columns, fields, matching) -> Result<Mapping, ShapeErrors>` strips each name through `column_name`
  and **collects every violation**. `ShapeErrors` is a non-empty list of `ShapeError`s, a `thiserror` enum. Errors carry
  **field indices** into the `fields` slice and 0-based column indices, not names: the message text is rendered by the
  caller, because the binding has the `RowShape` keys and `type_name` and the checker has its own field records.

  | `ShapeError` | When | Checker rule |
  |---|---|---|
  | `UnexpectedColumn { index, name }` | `ByName`: a column no field's names contain | `unexpected-column` |
  | `MissingColumn { field }` | `ByName`: a required field with no column | `missing-column` |
  | `DuplicateColumn { field: Option<usize>, indices }` | `ByName`: a name on two or more columns, or two accepted names of one field. `field` is `None` when no field has the name (`?column?`) | `duplicate-column` |
  | `AmbiguousColumn { index, name, fields }` | `ByName`: one column in the names of two or more fields | `ambiguous-column` |
  | `Arity { min, max, actual }` | `Positional`: the count is outside the required-to-total range | `arity-mismatch` |
  | `NameMismatch { position, field, column }` | `Positional`: position *i*'s name differs, reported for each position | `column-name-mismatch` |

  If any field's names are `Unknown`, a column that matches nothing passes through and the four `ByName` errors are not
  raised, for the whole model. `Positional` runs the name rules only when the arity is right. Issues are ordered by
  column index, then `MissingColumn` by field index. A `duplicate-column` sorts by its second (reported) column index,
  which is also its JSON `column_index`; an `ambiguous-column` sorts by its column. A name on two or more columns is a
  `DuplicateColumn` whether or not a field has that name, and its columns are not also `UnexpectedColumn`.
- `Mapping` holds, for each column, the field it maps to or a pass-through, and the optional fields that no column
  fills (the absent fields: the field indices that are not in the mapping).

**`rowx-postgres/src/exec.rs`.** `Shape` gains `fields: Option<&[FieldSpec]>` and `matching`. For a named shape, `mask`
is per field.

- **One source of truth.** `RowShape` (its per-field flags, bit 0) is the only place the nullable mask lives. The
  binding derives `Shape.mask` (per field), `fields` and `matching` from it, and `check_shape` skips the mask-length
  check when `fields` is set. The per-column NULL mask and the `Interval` flag (bit 1) come from the `Mapping`.
- `check_shape` calls `map_columns` when `fields` is set, in both the cached-statement re-prepare condition and the
  final check in `open()`, next to the arity check. It returns the `Mapping`. A mismatch is reported before execution,
  even for an empty result, and a stale cached statement is re-prepared before the error is raised, exactly as for an
  arity mismatch.
- `fetch` returns the `Mapping` with the rows, and `RowBatches` carries it. `check_nulls` uses a per-column mask derived
  from it: a mapped column takes its field's flag, and a pass-through column counts as nullable.
- A mapping failure is `Error::Shape { errors, columns }`, which maps to `RowShapeError` in
  `rowx-postgres-py/src/errors.rs`; the binding renders the message from the indices and `columns`, naming the row type
  and every error. A NULL in a non-nullable mapped field is `Error::NullField { column, field, row, name }`. A named
  shape must carry a full per-field mask, or the call is an `Interface` error.

**`rowx-postgres-py/src/exec.rs`.**

- **`RowShape`** is a pyclass exported from `_core.postgres`. It is immutable, and Python builds it once per plan, as
  `RowShape(ctor, form, matching, type_name, fields, /, *, validate=True)`. It holds:
  - `ctor`: the class, or for a pydantic model its `__pydantic_validator__` (a pydantic dataclass uses the class);
  - `form`: `positional`, `keyword`, `dict` or `validator`; the `validator` form records which call it makes.
    `validate=True` calls `ctor.validate_python(dict)` (a `BaseModel`, with `ctor=__pydantic_validator__`), and
    `validate=False` calls `ctor(**dict)` (a pydantic dataclass, with `ctor=cls`), because a `strict=True` pydantic
    dataclass rejects a dict in `validate_python`;
  - `matching`, and `type_name`;
  - for each field: an interned key `PyString`, the accepted names (or unknown), `required`, and flags (bit 0
    nullable, bit 1 `Interval`).
- **`Layout`** gains `form: RowForm`. The name avoids a clash with `rowx_postgres::exec::Shape`. `RowForm` is
  `Scalar`, `Tuple` (both unchanged) or `Named(Py<RowShape>)`. The `Interval` flag of a column comes from the field it
  maps to.
- **Construction** runs in the single GIL pass, from the `Mapping`. Cells go directly into the argument tuple, the
  kwargs dict or the row dict, and there is no intermediate tuple except for the positional form.

  | Form | Row |
  |---|---|
  | `positional` | one `PyTuple` in field order, then `ctor(*args)` |
  | `keyword` | a kwargs dict, without the absent optional fields, then `ctor(**kwargs)` |
  | `dict` | the row is the dict itself, without the absent `NotRequired` keys |
  | `validator` | `ctor.validate_python(dict)` for a model, `cls(**dict)` for a pydantic dataclass; the dict is keyed by column name and includes pass-through columns |

  The `keyword` form may call a dataclass positionally when it has no keyword-only fields and nothing is absent, if the
  benchmark shows a gain.
- **Stream layout.** `exec::stream` returns the `Mapping` with the `RowBatches`. The layout is fixed for the stream, and
  a mapping error is raised from the first `__anext__`.
- **Errors.** An exception from the constructor or validator becomes `RowConstructError`, with `type_name`, a 0-based
  `row_index` (the position in the returned rows, counted across stream batches, and always 0 for `one` and `optional`),
  and the original as `__cause__`. It wraps `Exception` only. In `all` and `one` the NULL checks run while the rows are
  collected, before any constructor runs. In a stream it is handled like a
  `DecodeError`: `_next()` returns the rows before the failing one, then raises. `RowConstructError(DecodeError)` is
  registered in `rowx-py-core/src/errors.rs` and mapped in `rowx-postgres-py/src/errors.rs`.
- **`stream.rs`** builds the stream's layout from the mapping of the first batch, because the handle exists before
  anything is prepared.
- **`map_columns(columns, fields, positional)`** is a `_core.postgres` function for the checker. `fields` is a list of
  `(names | None, required)`, so the checker needn't build a `RowShape`. It returns
  `tuple[list[int | None], list[ShapeIssue]]`: for each column, the field index or `None` for a pass-through (the absent
  optional fields are the field indices not in the mapping), and the issues.
  - **On failure the mapping is partial**, not empty. By name, a column maps to the one field that accepts its name,
    or to `None` when no field or several fields do (the columns of a `duplicate-column` keep the field index); by
    position, every entry is `None`. The checker uses it to run the type and nullability rules on cleanly mapped
    columns, and skips the columns named in `duplicate-column` and `ambiguous-column`.
  - `ShapeIssue` is a frozen pyclass with `kind` (the rule name: `"missing-column"`, `"unexpected-column"`,
    `"duplicate-column"`, `"ambiguous-column"`, `"column-name-mismatch"`, `"arity-mismatch"`), `columns: list[int]`
    (0-based), `fields: list[int]` (empty when no field), `name: str | None` (the stripped column name), and, for arity,
    `min`, `max` and `actual` (`None` otherwise).

**Acceptance.** The mapping cases are `cargo` tests in `row_shape.rs` and the same cases through `map_columns`. DB tests
cover a mapping error on an empty result, the re-prepare after a table gains a column, a NULL in a mapped non-nullable
field in `fetch` and in `RowBatches`, and a constructor that raises in `all`, `one` and `fetch` (earlier rows are
yielded, `row_index` is correct across a `batch_size=2` boundary, and the connection stays usable).

**Performance.** `bench/fetch_100k.py` gains a variant for each form (dataclass with `slots=True`, NamedTuple,
TypedDict, pydantic `BaseModel`, pydantic dataclass) next to the tuple baseline. Their numbers are recorded, with no
hard threshold. The only hard gate is no regression in the tuple and scalar paths.

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
   - A cancel ends the stream. The binding returns no more rows (`_next()` returns `[]`), and
     the producer keeps reading to the end of the statement, discarding rows, so it never
     blocks on `send` and the settle task's CancelRequests find the statement still running.
     Closing or dropping a cancelled stream does not stop the producer early for the same
     reason.
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

## Codec (`rowx-postgres-py/src/codec/`, `rowx-postgres/src/wire/`)

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
wire parsers in `rowx-postgres/src/wire/` produce Rust values, and `rowx-postgres-py/src/codec/` turns them
into Python objects.

- Constructors for `Decimal`, `UUID`, `datetime`, `date`, `time`, `timedelta` and
  `timezone.utc` are cached.
- jsonb strips its version byte, then calls `json.loads`.

**Logging.** tokio-postgres's `log` debug output prints parameters through `Debug`. The
parameter wrapper's `Debug` impl **MUST** redact values.

**Performance bar (v1).** Fetching 100k rows of (int4, text, timestamptz) with a release
build **should** take no longer than asyncpg does on the same machine. The spike measured
35.7 ms against asyncpg's 46.5 ms. This is tracked by a non-gating benchmark job.

**Named row types.** `bench/fetch_100k.py` times `all()` over the same 100k rows with each named form next to the tuple
and scalar baselines. The figures are the median of five runs of the script (each the median of seven timed runs) on a
release build on macOS, with Postgres 18 in Docker. There is no threshold: only the tuple and scalar paths are gated.

| Row type | `all()` (ms) |
|---|---|
| scalar `int` | 13.7 |
| `tuple[int, str, datetime]` | 54.5 |
| `TypedDict` | 60.1 |
| `@dataclass(slots=True)` | 69.0 |
| `NamedTuple` | 68.3 |
| pydantic `BaseModel` | 112.2 |
| pydantic dataclass | 143.7 |

asyncpg fetches the same rows in 50.8 ms. A `TypedDict` costs one dict per row, and a dataclass or NamedTuple one
Python-level constructor call. A `BaseModel` adds validation (`validate_python`), and a pydantic dataclass calls its
generated `__init__` with keyword arguments, which validates the same fields.

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

`state.rs` lives in `rowx-core/src/`, generic over the backend. `rowx-postgres` only supplies the Postgres connection.

The states are `Idle | Busy | Settling | Broken | Closed`, held in an atomic and changed by
compare-and-swap in the synchronous part of each `#[pymethods]` call.

| Call arrives while | Result |
|---|---|
| `Idle` | → `Busy`, proceed |
| `Busy` | raise `ConnectionBusyError` immediately (asyncpg semantics; no queueing) |
| `Settling` | await the settle notification, then retry the claim |
| `Broken` / `Closed` | raise `InterfaceError` |

Decision: there is one operation per connection, and the user **MUST** own the connection from a single task. The
transaction helper ([Transactions](#transactions)) is the supported way to run a transaction. Raw `BEGIN`/`COMMIT`
beside the helper is unsupported, and the docs say so.

The claim, state machine and settle logic live in `rowx-core` and are generic over a small backend trait (send a cancel
request, an is-alive check, with `Send` futures). `rowx-postgres` implements it, and the pool's `Manage` trait reuses
it.

**Leases.** The Idle → Busy claim also checks the lease ([Leases](#leases)) in the same compare-and-swap.

## Cancellation (`cancel.rs`)

The claim/settle and guard plumbing (`cancel.rs`) lives in `rowx-core/src/`. `rowx-postgres/src/cancel.rs` holds only
the backend implementation: the Postgres CancelRequest over the same TLS settings, and the is-alive check.

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

A cancelled statement inside `BEGIN` aborts the transaction, which then gives 25P02. Outside the helper the user
must `ROLLBACK`; this is documented. The helper rolls back after settling ([Transactions](#transactions)).

**Acceptance:**

- Cancelling `pg_sleep(3)` frees the backend. A poll of `pg_stat_activity` on another
  connection shows the sleep gone within 2 s.
- The next query on the same connection succeeds.
- In a 300-iteration stress test, a ~5 ms query is cancelled at a random point and a query
  follows immediately. **Zero** follow-ups may fail.

## Pool

`rowx.postgres.Pool` is backed by `rowx-core`'s pool, with a Postgres connector in `rowx-postgres`. The binding is in
`rowx-postgres-py/src/pool.rs`.

**Implementation: our own pool in `rowx-core`** (a FIFO-fair semaphore, a LIFO idle stack, a release task and a
maintenance loop), not `deadpool::managed`. The spike (#75) found both viable; the decision log (C25) has the checklist
and the numbers. `deadpool-postgres` is rejected: its statement cache conflicts with rowx's cache and its cancel/settle
path. Everything below describes behaviour only.

```text
Pool(options, /, *, min_size=0, max_size=10, acquire_timeout=30.0, idle_timeout=600.0, max_lifetime=1800.0,
     max_queries=None, ping_after=30.0, init=None, setup=None, reset=None, reset_query=RESET_QUERY)
Pool.open() / close(*, timeout=None) / terminate() / acquire() / release(conn) / transaction(...) / expire_connections()
Pool._fetch / _execute / _stream      # the Executor SPI
Pool.size | idle | waiting | min_size | max_size | closed
```

### Construction and lifecycle

- The constructor does no IO and is safe at module level. `await pool.open()` or `async with pool` warms the pool to
  `min_size`, and the **first acquire opens the pool implicitly**.
- The pool is not bound to an event loop, because IO runs on the shared tokio runtime.
- **Validation** raises `ValueError`: `1 ≤ max_size`, `0 ≤ min_size ≤ max_size`, timeouts ≥ 0, and `max_queries ≥ 1`
  when set.
- **Fork.** The pool records its pid. Any use in a forked child raises `InterfaceError` ("create the pool after
  fork"). The guide covers gunicorn `--preload`.
- **Weak references.** Background tasks hold `Weak` references to the pool, so a pool that is garbage-collected without
  `close()` doesn't leak.
- `close(*, timeout=None)` stops new acquires, fails waiters at once, waits for checked-out connections to be released
  and closes every connection. When the timeout runs out, it terminates the rest. `terminate()` closes everything
  immediately, and is synchronous.
- **Introspection:** `size`, `idle`, `waiting`, `min_size`, `max_size` and `closed`. Acquire wait time is recorded for
  the benchmark.

### Acquire

- Waiters are served **FIFO** (fair). Idle connections are reused **LIFO**, so hot connections get reused and cold ones
  age out.
- Waiting longer than `acquire_timeout` raises `PoolTimeoutError`. A closed pool raises `InterfaceError`. Pool
  operations never raise `ConnectionBusyError`.
- **Connect retries.** A failed connect is retried with backoff until `acquire_timeout` runs out (as sqlx does), and
  each attempt is bounded by `connect_timeout`. Background `min_size` maintenance also retries with backoff.
- **Capacity** counts connections that are resetting or settling.
- **A cancelled waiter never runs its query.** pyo3-async-runtimes drops the Rust future late, so the done callback sets
  a "cancelled" flag, which is checked right after the acquire. A permit granted in that window goes straight back to
  the pool.

### Liveness

There is no round trip on the hot path.

- Acquire discards and replaces Broken connections (where the connection task has ended).
- A ping runs only when a connection has been idle longer than `ping_after`.
- A query that fails with an IO error is **not** retried, because it may not be idempotent.

### Leases

- Every checkout gets a lease (a generation number). Claiming the connection checks the lease in the same
  compare-and-swap as Idle → Busy ([Connection state machine](#connection-state-machine-staters)).
- Streams and Transactions built on a pooled `Connection` inherit its lease. So a stream created inside
  `async with pool.acquire()` and iterated after the block raises `InterfaceError`. It never runs on another task's
  connection.
- **Release revokes the lease first.** If the connection is still Busy, release cancels and settles it, as `close()`
  does.
- `pool.acquire()` is awaitable and an async context manager, and yields a `Connection`. Release detaches it, so later
  use raises `InterfaceError`. `conn.detach()` takes it out of the pool permanently.

### Release

- `RESET_QUERY` is asyncpg's `SELECT pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; RESET ALL;`. There is no
  `DISCARD ALL`, so the per-connection statement cache survives checkouts. Other state that `RESET ALL` keeps
  (`SET ROLE`, `SET SESSION AUTHORIZATION`, temp tables) is documented.
- **`ROLLBACK` is prepended only when needed.** An unconditional one logs a server WARNING (25P01) on every release, and
  tokio-postgres drops the ReadyForQuery transaction status. So rowx keeps a conservative "maybe in a transaction" flag,
  set by any of:
  - a helper transaction depth above 0;
  - a cancelled operation;
  - any 25P02 error;
  - SQL whose first keyword is `BEGIN`, `START` or `SAVEPOINT`.
- When the flag is set, `ROLLBACK` is sent even with a custom `reset_query` or `reset_query=None`. As in asyncpg, an
  open transaction always ends. It takes one round trip, and **MUST** be correct from idle, in-transaction and
  failed-transaction states.
- `reset_query=None` turns the reset query off, and `reset=` adds an async hook that runs after it.
- **Plain-awaited connections:** `await pool.release(conn)`, or `await conn.close()` on the lease.
- **Early exit from a pooled stream** drains the remaining rows before the reset runs. Pool size stays correct while it
  drains.
- **Background release.** With no hooks set, release runs off the caller's path: the result, or `__aexit__`, returns
  immediately, and the connection becomes idle only after the reset finishes. Release can't be cancelled from Python,
  and a failed reset closes the connection.
- A cancelled operation's connection settles ([Cancellation](#cancellation-cancelrs)) and resets before it becomes idle
  again.
- **Schema epoch.** A stale-plan error (`0A000`, `26000`) bumps a pool-wide epoch. Each connection checks the epoch at
  checkout and drops its statement cache if it is behind, as asyncpg does. Otherwise every connection's first use after
  a migration inside `pool.transaction()` would abort that transaction.

### Retirement and maintenance

- A connection is closed instead of reused once it passes `max_lifetime` or `max_queries`, or after
  `expire_connections()` (a generation bump, as in asyncpg).
- Idle connections above `min_size` are closed after `idle_timeout`, and `min_size` is maintained in the background.

### Hooks

Hooks are async Python callables taking a `Connection`. They run **only on a caller's event loop**, never from
background tasks.

- **`init`** runs lazily, at a connection's first checkout. A connection that `open()` or `min_size` maintenance creates
  is marked "init pending".
- **`setup`** runs before a connection is handed out.
- **`reset`.** When any hook is set, `__aexit__` awaits the `reset` hook (and the reset query) on the caller's loop,
  then hands the connection back to Rust. Release is then on the caller's path.
- **Errors.** A hook that raises discards the connection. An `init` or `setup` failure is raised from acquire, and pool
  capacity isn't leaked.
- The `Connection` passed to a hook is invalidated when the hook returns.

### Fast path

With no hooks set, `query.one(pool)` is one Rust future (acquire, run, deliver, release). It costs one Python await, the
same as on a Connection. `fetch(pool)` acquires at the first `__anext__`, holds the connection until the stream ends or
is closed, then releases it.

### Cancellation on a pool

Cancelling a task awaiting a pooled operation cancels and settles as for a Connection. The connection then resets before
it is reusable, and the lease is revoked. A cancelled waiter is covered under [Acquire](#acquire).

### Server-side poolers

PgBouncer, pgcat and Supavisor are documented only, with no CI job. Transaction mode needs `statement_cache_size=0`, or
PgBouncer ≥ 1.21 with `max_prepared_statements`.

### Performance bar

Non-gating. At 64 concurrent tasks, `query.one(pool)` throughput is at least asyncpg's `pool.fetchrow`. Both pools are
warmed and use identical `min_size` and `max_size`. The cost of the reset step is reported separately.

## Transactions

```text
Connection.transaction(*, isolation=None, readonly=False, deferrable=False) -> Transaction
Pool.transaction(...)                  -> Transaction     # holds a connection for its duration
Transaction.transaction()              -> Transaction     # SAVEPOINT; isolation arguments raise InterfaceError
Transaction.commit() / rollback()      # awaitable; end it early
```

- **SQL.** The outermost block runs `BEGIN` (with `ISOLATION LEVEL …`, `READ ONLY` and `DEFERRABLE` as requested). A
  nested block runs `SAVEPOINT rowx_sp_<n>`, with `n` taken from a per-connection **depth counter**. The nested exit
  runs `RELEASE SAVEPOINT` or `ROLLBACK TO SAVEPOINT`.
- **A Transaction is an Executor.** Queries run on its connection inside the block are part of the transaction (asyncpg
  semantics). Raw `BEGIN`/`COMMIT` alongside the helper is unsupported.
- **Exit.** A clean exit commits. An exception or a cancellation rolls back, after settling. Using a Transaction after
  it ends raises `InterfaceError`.
- **The ending statement can't be cancelled.** The `COMMIT`/`ROLLBACK` in `__aexit__` runs in a detached task, so a
  second cancel or `asyncio.timeout` can't leave the transaction open. If the caller is cancelled while a `COMMIT` is in
  flight, the outcome is unknown, so it raises and the connection is discarded.
- **The "failed" flag.** Any error inside the block sets a per-transaction flag, and `ROLLBACK TO SAVEPOINT` clears it.
  A clean exit while the flag is set raises `InterfaceError` instead of letting `COMMIT` silently roll back.
- **A failed `COMMIT`** (for example `40001`) raises `DatabaseError`. The connection is left Idle and outside any
  transaction.
- The depth counter feeds the "maybe in a transaction" flag ([Release](#release)).

**Acceptance:**

- A clean exit commits, and an exception rolls back. Savepoints roll back independently of the outer transaction.
- Cancelling a task mid-statement rolls back, and the connection is reusable.
- A second cancel during `__aexit__` does not leave the transaction open.
- A clean exit with the failed flag set raises `InterfaceError`.
- A serialization failure on `COMMIT` raises `DatabaseError`, and the connection stays usable.
- `pool.transaction()` returns the connection reset and idle.

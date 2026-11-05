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
- **`channel_binding`** (`disable`, `prefer`, `require`) is a typed `ConnectOptions` field and a DSN key. It is parsed
  from the DSN into the typed field and never carried in `extra`, so there is one source: an `extra` key
  `channel_binding` and an invalid value are each an `InterfaceError`. rowx maps it to tokio-postgres's
  `ChannelBinding`; tokio-postgres-rustls supplies the `tls-server-end-point`
  digest, so SCRAM-SHA-256-PLUS can be negotiated. With `require`, the client **MUST NOT** send a password or SASL
  response for cleartext, MD5 or SCRAM-SHA-256 without `-PLUS`: the connection fails first. `require` with
  `sslmode=disable` is an `InterfaceError` at parse time, whether it came from the DSN or the field.
  tokio-postgres-rustls computes `tls-server-end-point` only for certificates signed with SHA-1, SHA-256, SHA-384 or
  SHA-512 RSA, or ECDSA-SHA256 or ECDSA-SHA384 (not Ed25519 or ECDSA-SHA512), so `channel_binding=require` fails
  against a server with another kind of certificate. The default `sslmode` stays `prefer`, for parity with libpq,
  sqlx and asyncpg, and production use is `sslmode=verify-full&channel_binding=require`
  ([Security](security.md#tls-and-authentication)). `verify-full` and `require` **MUST NOT** fall back to plaintext
  when the server answers `N` to SSLRequest, and a cancel request ([Cancellation](#cancellation-cancelrs)) follows the
  connection's TLS mode. `require_auth` is a non-goal, because tokio-postgres has no support for it.
- `connect_timeout` bounds each connect attempt.
- **Redaction** is by secret value, not by DSN syntax. Every occurrence of the password, and of the value of any `extra`
  key containing `password` (such as `sslpassword`), is redacted in any exception message, `repr`, wrapped cause's
  `repr()` and checker diagnostic, whichever DSN form carried it (URL userinfo, `?password=`, or key=value
  `password=`). Redaction replaces the decoded value and every raw spelling of it in the DSN text (percent-encoded,
  quote- or backslash-escaped), and also masks URL userinfo and `password=` tokens syntactically, so a DSN that fails to
  parse is still covered. Empty secrets are skipped. `ConnectOptions.__repr__` shows `extra` values as `'<redacted>'`.
- `ping()` sends an empty simple query. `reset()` runs `RESET_QUERY` ([Release](#release)).
- `close()` works as follows:
  - If the state is `Busy` or `Settling`, it cancels and settles first ([Cancellation](#cancellation-cancelrs)).
  - It then drops the client and awaits the connection task. Close is idempotent.

### Option validation

Options are trusted configuration, but they reach the server as startup text, so `dsn.rs` validates them when it
parses a DSN or converts a `ConnectOptions`. Anything else is an `InterfaceError`.

- **`server_settings` keys** **MUST** match the GUC grammar `^[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*)*$`,
  with at most 63 bytes per dot-separated segment. Because `server_settings` travel inside the startup `options` as
  `-c k=v`, a tab or newline in a key would be split by the server into extra `-c` options. This is reachable only
  through programmatic `ConnectOptions`, since DSN-parsed keys can't hold whitespace.
- **DSN `options` names.** In an `options` string, both `--long-option` and `-c name` map `-` to `_` the way Postgres
  `ParseLongOption` does, and the name then follows the GUC grammar. `--search-path=x` and `-c search-path=x` are
  `search_path`.
- **`extra` keys** **MUST** match `^[a-z_]+$`, which every libpq keyword does. They are re-parsed as conninfo.
- **Values.** A NUL or other C0 control character in any option value (`password`, `options`, `server_settings` values,
  `extra` values) is an `InterfaceError`. The message names the key and **never** the value.

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

### Binary arrays and ranges

Arrays and ranges are **always** sent in binary format. `array_enc.rs` and `range.rs` produce the binary form for every
element and bound, and no element or bound is ever rendered as text. The invariant is that no untrusted input can
change the element and bound structure of an array or range parameter
([Threat model](security.md#threat-model)): with a text literal, a quote or backslash that a client-only encoding
reinterprets could close an element early. `wire::array::encode_literal` and `wire::range::encode_literal` do not
exist.

- The array and range encoders do not consult `encode_format` for the element form. `TYPE_TABLE` is not edited, and
  `accepted_params` is unchanged, so the checker is unaffected.
  - **Table versus what is sent.** `encode_format` stays `Text` for scalar `numeric`, `timetz`, `json`, `jsonb` and
    `enum`, and for `numrange` (and any other range whose table row is `Text`). Nothing in `codec/range.rs` reads it.
    `scripts/gen-types-doc.py` publishes the `array` row and every range row as binary in `docs/types.md`, whose notes
    say an array or range is binary even when the scalar form is text.
- **Scalar parameters are unchanged.** A scalar has no literal grammar, so a text-format scalar (`numeric`, `timetz`)
  can corrupt only itself. `json.dumps` stays ASCII-only (`ensure_ascii=True`), which a test pins.
- **Elements and bounds whose scalar form is text get binary element encoders:**
  - `numeric` through `Numeric::from_decimal_parts(sign, digits, exponent)` in `wire/numeric.rs`. A `Numeric` was built
    only by `decode` before, so this constructor is new. It handles:
    - a positive exponent: `Decimal('1E+100000')` becomes zero-padded digit groups, bounded by the `i16` weight, and
      past that bound it is an `EncodeError`;
    - a scale above `MAX_SCALE` is an `EncodeError`;
    - quiet `NaN` and `±Infinity` encode as the special values;
    - a signalling NaN is an `EncodeError`;
    - digits of a `Decimal` are read through the unbound `decimal.Decimal.__str__`, so a subclass's `__str__` or
      `as_tuple` cannot choose the value. The text is parsed in Rust and every digit is validated as 0 to 9.
      `capitals = 0` prints a lowercase exponent, which the parser accepts. `clamp`, `prec`, `rounding` and
      `Emax`/`Emin` do not change `__str__`;
    - an `int` outside `i64` is converted through `Decimal(int)` and that same string, not `str(int)`, which is capped
      at the int-to-str digit limit. An int past that limit still encodes. A conversion that fails is an `EncodeError`;
    - the scalar numeric parameter text is the `digitsE±exp` form built from those parts. It can appear in server logs
      and error DETAIL. Decoded results are unchanged;
  - `timetz` through `wire::temporal::encode_timetz`: microseconds since midnight plus the zone offset in seconds
    *west* of UTC;
  - `json` as UTF-8, and `jsonb` as `0x01` followed by UTF-8;
  - an enum label as UTF-8, under the element OID taken from the prepared statement's `Kind::Array(inner)`;
  - `uuid` from a `str`, parsed and validated to 16 bytes on the client.
- Nested lists **MUST** be rectangular; a ragged list raises `EncodeError`. NULL elements use the binary NULL length.
- Every encoder is strict, with no `str()` fallback.

**Decode.** A custom `FromSql`, `Raw<'a>(Option<&'a [u8]>, &Type)`, accepts every type. Pure
wire parsers in `rowx-postgres/src/wire/` produce Rust values, and `rowx-postgres-py/src/codec/` turns them
into Python objects.

- Constructors for `Decimal`, `UUID`, `datetime`, `date`, `time`, `timedelta` and
  `timezone.utc` are cached.
- jsonb strips its version byte, then calls `json.loads`.

**Decoders under an untrusted server.** The decoders parse server bytes, which a MITM controls when `sslmode` is weaker
than `verify-full`. They **MUST** return `DecodeError` for malformed input and never panic: a panic surfaces in Python
as `PanicException`, a `BaseException`.

- `wire/mod.rs` has `#![deny(clippy::indexing_slicing, clippy::arithmetic_side_effects)]`, scoped to the module and not
  the workspace. The lints cover `wire/` only. The PyObject decoders in `crates/rowx-postgres-py/src/codec/` and the
  describe and explain parsing are covered by the panic-boundary test and fuzzing instead, and the panic-boundary test
  decodes through Python so that it exercises `codec/`. Code uses `get` and `checked_*`, not `#[allow]`. The array
  decoder bounds `ndim` and uses `checked_mul` for the element count.
- **Fuzzing.** Every `wire/` decoder has a `cargo-fuzz` target, as do arrays of each element family, the `explain`
  plan JSON reader and `dsn::parse`. Every target runs with `-rss_limit_mb`, so an over-allocation fails. Encoders have
  `decode(encode(x)) == x` targets ([Testing](testing.md)).
- **Protocol length limits** (RUSTSEC-2024-0363 class). A length that doesn't fit the protocol's `i32` (a parameter or
  message of 2³¹ bytes or more) or `u16` (a parameter count above 65535) **MUST** raise `EncodeError` or
  `InterfaceError` before anything is written, and the connection stays usable. A NUL in the SQL text fails closed the
  same way, and the Python compiler rejects both earlier ([Compile limits](api.md#compile-limits)).
- **Internal SQL** names `pg_catalog` objects explicitly, so `search_path` can't redirect a statement rowx builds.

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

### Session encoding watch

tokio-postgres forces `client_encoding=UTF8` at startup, but it ignores a later `SET client_encoding = 'SJIS'` run by
application SQL, and rowx would keep sending UTF-8, silently storing text in the wrong encoding. The watch is a backstop
against that drift. It lands after binary arrays and ranges (#219), so it is never the only protection. It is not what
protects array structure: binary arrays and ranges do ([Binary arrays and ranges](#binary-arrays-and-ranges)), so a
statement that slips through can suffer data drift but not element injection.

- **Watching.** `conn.rs` drives the tokio-postgres `Connection` with
  `poll_fn(|cx| { let r = conn.poll_message(cx); check(&conn); r })`, so the check runs on **every** poll, including
  `Pending`. This matters because `ParameterStatus` is consumed inside `poll_read` and is never returned from
  `poll_message`.
- **Taint.** `UTF8` and `UNICODE`, compared ASCII case-insensitively (`utf8`, `Utf8`, `unicode`), both count as UTF-8,
  because Postgres reports `SET client_encoding = 'UNICODE'` under that name. Any other `client_encoding`, or
  `standard_conforming_strings` turning `off`, stores a taint reason in an `AtomicU8` next to `broken`. A
  server-supplied encoding name is bounded to 64 characters and made printable before it is put in a message.
- **Failing.** The taint is a `Drift` error, mapped to `InterfaceError`. It does not move the connection state machine
  to `Broken`. `Connection::client()` and `parts()` fail with a message that names the encoding, for example
  "the session changed client_encoding to SJIS; rowx requires UTF8". The pool's `is_broken` then discards the
  connection through its existing `client().is_err()` path, so a drifted connection never serves another checkout.
  A `SET client_encoding = 'UTF8'` when it was already `UTF8` never taints the connection. The statement that changes
  the encoding fails once its response is fully consumed (`settled` turns `Ok` into `Drift`), so `execute` of the
  `SET` itself raises. A change undone inside the same statement, so the last `ParameterStatus` is UTF-8, is not
  reported and the connection stays usable.
- **Sticky.** The taint lasts for the life of the connection: a later `RESET ALL` or `SET client_encoding = UTF8`
  doesn't clear it.
- **Discard paths.** `is_broken` uses `try_lock`, so a connection whose lock is held is not reported broken there. The
  release path's reset calls `client()`, which fails on a tainted connection, and the pool discards it.
- **Role and database defaults.** A server-, database- or role-level default of `client_encoding` other than `UTF8` is
  overridden by tokio-postgres's startup value. But `standard_conforming_strings = off` set at role or database level
  taints the connection at connect, so the first call raises `InterfaceError`.
- **Residual window.** The server sends `ParameterStatus` before the `SET`'s `CommandComplete`, but `poll_read` may
  forward `ReadyForQuery` to a waiting client on another worker before the check runs. Only a statement already queued
  behind the `SET` can slip through. Several `ParameterStatus` messages processed in one poll are observed only as the
  final value. That is drift-only, because arrays and ranges are binary.
- **Cost.** A legitimate `SET standard_conforming_strings = off` now costs a connection. This is documented.
- `DateStyle` and `IntervalStyle` are not watched, because every decoder rowx uses is binary.

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

- `RESET_QUERY` is asyncpg's reset with the authorization reset first, the function qualified, and `DISCARD TEMP` added:
  `RESET SESSION AUTHORIZATION; RESET ALL; SELECT pg_catalog.pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; DISCARD TEMP;`.
  There is no `DISCARD ALL`, so the per-connection statement cache survives checkouts.
  - **Order and qualification.** The batch runs under whatever session state the previous checkout left. So the
    authorization reset comes first, `RESET ALL` second (it restores `search_path`), and only then is a function
    called, and that function is `pg_catalog`-qualified.
  - **`RESET SESSION AUTHORIZATION`** resets both the session user and the role. `RESET ALL` does not: Postgres marks
    `role` and `session_authorization` as excluded from `RESET ALL`, so it would keep `SET ROLE` and
    `SET SESSION AUTHORIZATION`. This corrects #214's claim that `RESET ALL` restores `SET ROLE`
    ([decision log](decision-log.md#security-hardening-2026-11-03), S11).
  - **`DISCARD TEMP`** drops the session's temp tables. `pg_temp` is searched first for relations, so a temp table left
    by one checkout would otherwise shadow an application table for the next. This is a deliberate divergence from
    asyncpg, which leaves them. It runs inside the multi-statement simple query (verified on Postgres 18). A cached
    statement that referenced a dropped temp table fails on its next use like any statement against a dropped table.
  - **`DEALLOCATE ALL` is not added**, because rowx's own protocol-level prepared statements are cached per connection
    and it would destroy them. SQL-level `PREPARE` is therefore **unsupported across checkouts**: a statement prepared
    with `PREPARE` may survive in the session or not. Use bound parameters.
  - `RESET ALL` returns custom placeholder GUCs (`app.tenant`) to the empty string, so their values don't carry over.
  - `RESET ALL` restores `client_encoding`, `search_path` and other settings, but not the role or session
    authorization, which `RESET SESSION AUTHORIZATION` covers. The
    [session encoding watch](#session-encoding-watch) catches an encoding change that happens during a checkout.
  - `reset_query=None` **is unsafe for multi-tenant pools**: nothing clears the role, `search_path`, temp tables or
    advisory locks. The guide says so.
- **`ROLLBACK` is prepended only when needed.** An unconditional one logs a server WARNING (25P01) on every release, and
  tokio-postgres drops the ReadyForQuery transaction status. So rowx keeps a conservative "maybe in a transaction" flag,
  set by any of:
  - a helper transaction depth above 0;
  - a cancelled operation;
  - any 25P02 error;
  - SQL whose first keyword is `BEGIN`, `START` or `SAVEPOINT`.
- **The flag is a heuristic, so the reset carries an idle guard.** The first-keyword scan skips leading `;`, empty
  statements, a `--` comment ended at `\n` or `\r`, and nested `/* */` comments, then looks at the first keyword. When
  the flag is clear, the guard is
  `SELECT 1 OPERATOR(pg_catalog./) (pg_catalog.now() OPERATOR(pg_catalog.=) pg_catalog.statement_timestamp())::pg_catalog.int4`.
  From idle, `now()` equals `statement_timestamp()`. Inside an explicit transaction it doesn't, so the guard fails and
  the connection is closed. The default `RESET_QUERY` is one batch: the guard, or `ROLLBACK;`, prefixed onto that query.
  A custom reset query is two round trips, the guard or `ROLLBACK` alone and then the custom query alone, because a
  multi-statement batch is an implicit transaction and `DISCARD ALL` or `VACUUM` cannot run inside one. `RESET_QUERY`
  text is unchanged. With `reset_query=None` and the flag clear, nothing is sent, so no guard runs, and
  `reset_query=None` remains unsafe (below). With the flag set and `reset_query=None`, only `ROLLBACK` is sent.
- When the flag is set, `ROLLBACK` is sent even with a custom `reset_query` or `reset_query=None`. As in asyncpg, an
  open transaction always ends. It takes one round trip, and **MUST** be correct from idle, in-transaction and
  failed-transaction states.
- `reset_query=None` turns the reset query off, and `reset=` adds an async hook that runs after it. With
  `reset_query=None` and a `reset=` hook, any `ROLLBACK` is sent before the hook runs, so the hook sees an idle
  connection.
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

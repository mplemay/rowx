# rowx — Implementation Tasks (v1)

This file breaks [`design.md`](design.md) (revision 2) into **48 small, verifiable tasks**.

- Each task should fit in one PR of about 400 changed lines or fewer, not counting
  fixtures and lockfiles.
- Each task lists dependencies, the files it owns, what to do, and an **acceptance check**:
  exact commands and the expected result.
- `§` references point into `design.md`. That doc is the contract (design §16).

---

## 0. Working agreement (for every agent)

### 0.1 Commands

These exist after T00, T01 and T03.

| Command | What it does |
|---|---|
| `just check` | All non-DB gates: `cargo fmt --check`, `cargo clippy --workspace --all-targets -- -D warnings`, `ruff check`, `ruff format --check`, `pyright`, `mypy src`, `cargo test --workspace --lib`, `pytest -m "not db"` |
| `just db-up` / `just db-down` | Start or stop `postgres:18` via `scripts/pg.sh`, printing `export ROWX_TEST_DSN=…` |
| `just test-rust` | `cargo test --workspace` (DB tests run if `ROWX_TEST_DSN` is set) |
| `just test-py` | `uv run pytest` |
| `just bench` | Release build plus `bench/fetch_100k.py` |
| `just fmt` | `cargo fmt` + `ruff format` |

- Never run a plain `cargo build` of the cdylib, because it doesn't link on macOS. Use
  `uv run …` or `uv run maturin develop`, and add `--release` for benchmarks.
- DB tests: run `export ROWX_TEST_DSN=$(scripts/pg.sh up)`. If the variable is unset, DB
  tests skip; with `ROWX_REQUIRE_DB=1`, as CI sets, they fail instead.

### 0.2 Definition of done

1. The acceptance check in the task passes locally, and CI is green on Linux (full) and
   macOS (non-DB).
2. The new tests are named in the PR description. Behaviour without a test is not done.
3. Changes stay inside the task's **Files** list. A small edit elsewhere is OK only for
   wiring, such as `mod` lines, `__init__` exports or `.pyi` additions, and must be called
   out in the PR.
4. No `unwrap()`/`expect()` in non-test Rust, except for documented invariants. Errors use
   `thiserror` in `rowx-pg` and are mapped to Python exceptions **only** in
   `crates/rowx-py/src/errors.rs`.
5. All public Python is fully typed (`pyright --strict` on `src/rowx`), and `_core.pyi` is
   updated with any new binding.
6. If the spec is ambiguous, add an entry to `docs/decisions.md` (date, task, question,
   decision, why) rather than guessing silently. Never edit `design.md` in a feature PR.

### 0.3 Conventions

- **Rust.**
  - `rowx-pg` has **no pyo3 dependency**.
  - Wire codecs in `rowx-pg/src/wire/` are pure functions over bytes, with unit tests.
  - The GIL is released around all IO (`Python::detach` in pyo3 0.28), and IO runs on the
    shared tokio runtime held in `rowx-py/src/runtime.rs`.
- **Python.**
  - 3.14 syntax and no runtime dependencies.
  - Checker models are `@dataclass(frozen=True, slots=True)`.
  - The checker never imports or executes user code.
- **Tests.**
  - DB tests are marked `@pytest.mark.db` and use the `schema` fixture, never `public`.
  - Timing tests are marked `@pytest.mark.timing` and synchronise by polling
    `pg_stat_activity`, never with `sleep`.
  - One test file per task area, so parallel tasks don't touch the same file.

### 0.4 File-ownership map

| Area | Paths | Owner tasks |
|---|---|---|
| Type-table metadata | `crates/rowx-pg/src/types.rs` | T05. Afterwards it is append-only, and only codec tasks flip `Codec::Unsupported` to real variants in their own rows. |
| Wire codecs (pure) | `crates/rowx-pg/src/wire/<family>.rs` | T13–T18, one file each |
| PyObject codecs | `crates/rowx-py/src/codec/<family>.rs`; `codec/mod.rs` is dispatch only | T08 creates `mod.rs`; T12–T18 add one arm per family |
| Driver | `crates/rowx-pg/src/{dsn,tls,conn,state,cancel,stmt_cache,exec}.rs` | T06, T08, T20–T23 |
| Describe | `crates/rowx-pg/src/{describe,explain}.rs`, `crates/rowx-py/src/describe.rs` | T29–T33 |
| Bindings | `crates/rowx-py/src/{lib,conn,exec,errors,runtime,types}.rs`; `lib.rs` only registers | T01, T05, T07, T08 |
| Python runtime | `src/rowx/{__init__,_query,errors,_template,_rowplan,_types}.py` | T02, T05, T09–T11 |
| Checker | `src/rowx/check/<module>.py` | T35–T44, one module each |
| Tests | `tests/<area>/…`, `tests/golden/<family>.py`, `tests/check/cases/<rule>.py` | the task that owns that area |

---

## 1. Dependency graph and waves

```text
P0  T00 ─┐  T01 ─┬─ T02 ─┐
         │       ├─ T03 ─┼─ T04
         │       └─ T05 ─┘
P1  T06 → T07 → T08 ─┐
    T09 ─────────────┼─→ T11  (VERTICAL SLICE)
    T10 ─────────────┘
P2a T12 → {T13,T14,T15} → {T16,T17} → T18 → T19
P2b {T20, T21 → T22, T23} ; T24(bench) ; T25(e2e) ; T26(typing) ; T27(conformance)
P2c T28 → T29 → T30 ; T31 → T32 → T33 → T34
P2d T35, T36, T37, T38, T39, T40, T43 (DB-free) → T41 → T42 → T44
P3  T45 → T46 → T47
```

- **Wave 0:** T00, T01 and T03 in parallel; then T02, T04 and T05.
- **Wave 1:** T06 → T07 → T08, with T09 and T10 in parallel; then T11.
- **Wave 2:** up to six agents, one per lane: codecs, driver, describe and checker.
  - The checker's DB-free tasks (T35–T40, T43) and T28–T30 only need P0, so they can start
    as early as Wave 1.
- **Wave 3:** T41, T42 and T44 (checker integration); T24 and T25.
- **Wave 4:** T45–T47.
- **Critical path:** T01 → T05 → T06 → T07 → T08 → T11 → T31 → T32 → T33 → T41 → T42 →
  T44 → T46 → T47.

---

## P0 — Foundation

### T00 · Agent guide, justfile, commit docs

- **Deps:** none · **Files:** `AGENTS.md`, `CLAUDE.md` (one line: `@AGENTS.md`), `justfile`, `.gitignore`,
  `docs/design.md`, `docs/tasks.md`, `docs/decisions.md` (empty template), `conversation.md`
- **Do:**
  - `AGENTS.md` holds §0 of this file (commands, definition of done, conventions, ownership
    map), plus a one-paragraph project summary and links to the docs.
  - `justfile` has the recipes from §0.1. They may stub to `echo TODO` until their tasks
    land.
  - `.gitignore` covers `target/`, `.venv/`, `*.so`, `.pytest_cache/` and
    `/.rowx-test-dsn`.
- **Accept:**
  - `just --list` shows `check test-rust test-py db-up db-down bench fmt`.
  - `git ls-files AGENTS.md CLAUDE.md docs/` lists all of them.

### T01 · Cargo workspace, toolchain, build chain

- **Deps:** none · **Files:** `Cargo.toml` (workspace), `Cargo.lock`, `rust-toolchain.toml`,
  `crates/rowx-pg/{Cargo.toml,src/lib.rs}`, `crates/rowx-py/{Cargo.toml,src/lib.rs}`, `pyproject.toml`,
  `src/rowx/_core.pyi`, `src/rowx/__init__.py`; delete `src/lib.rs`
- **Do:** apply design §5 exactly.
  - Set up the workspace, the pinned toolchain, and `pyo3` 0.28 with `abi3-py314`.
    `extension-module` must be enabled only through `[tool.maturin] features`.
  - Add `pyo3-async-runtimes` 0.28 with tokio.
  - Set maturin `manifest-path` and the uv `cache-keys` for `crates/**`.
  - Add `_core.ping() -> "pong"` and remove `hello_from_bin`.
  - Temporarily point `[project.scripts]` at `rowx.check.cli:main`. T02 creates it, so
    T01 adds a stub `main` in `src/rowx/check/cli.py` that prints the version.
  - If pyo3-async-runtimes 0.28 doesn't resolve against pyo3 0.28, **stop** and record the
    nearest compatible pair in `docs/decisions.md`.
- **Accept:**
  - `cargo test --workspace && cargo clippy --workspace --all-targets -- -D warnings`
    passes.
  - `uv sync && uv run python -c "from rowx import _core; print(_core.ping())"` prints
    `pong`.
  - Change the string to `pong2` in `crates/rowx-py/src/lib.rs` and re-run: it prints
    `pong2`, which proves the cache keys work.
  - `uv run maturin build --release` produces `*-cp314-abi3-*.whl`.

### T02 · Python skeleton, error hierarchy, tooling config

- **Deps:** T01 · **Files:** `src/rowx/{__init__,errors,_types,_template,_rowplan,_query}.py` (placeholders except
  `errors`), `src/rowx/check/{__init__,cli}.py`, `src/rowx/py.typed`, `pyproject.toml` (dev group,
  ruff/pyright/mypy/pytest config, markers `db` and `timing`, `asyncio_mode="auto"`), `tests/test_errors.py`
- **Do:**
  - Implement design §3.7 in full in `errors.py`. `DatabaseError.__init__(sqlstate,
    message, detail=None, hint=None, position=None)` stores all five as attributes.
  - `rowx check --version` prints the version and exits 0.
  - Pin the pyright and mypy versions.
- **Accept:**
  - `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run mypy src && uv run pytest -q`
    all pass.
  - `test_errors.py` asserts the full tree: `issubclass(ConnectionBusyError,
    InterfaceError)`, every class is a subclass of `rowx.Error`, and `rowx.Error.__bases__
    == (Exception,)`.

### T03 · Postgres test infrastructure

- **Deps:** T01 · **Files:** `scripts/pg.sh`, `scripts/gen-certs.sh` (stub, completed in T23), `tests/schema.sql`,
  `tests/seed_data.sql`, `tests/conftest.py`, `crates/rowx-pg/tests/common/mod.rs`, `crates/rowx-pg/tests/smoke.rs`,
  `crates/rowx-pg/Cargo.toml` (dev-deps), `justfile` (`db-up`/`db-down`)
- **Do:**
  - Implement design §10 provisioning and isolation.
  - `pg.sh up` runs `docker run -d -p 127.0.0.1::5432 … postgres:18 -c max_connections=200`.
    It waits on `pg_isready`, prints the DSN, and records the container ID in
    `.rowx-test-dsn`.
  - The pytest fixture `schema` creates `t_<hex>`, loads `schema.sql` into it, and yields a
    DSN with `options=-c search_path=t_<hex>`. The Rust helpers `common::test_dsn()` and
    `common::fresh_schema()` do the same.
  - `schema.sql` contains everything listed in design §10, including the `NOT VALID`
    column, an enum, a domain, a domain array and citext.
  - Apply the `ROWX_REQUIRE_DB` skip/fail policy.
- **Accept:**
  - `export ROWX_TEST_DSN=$(scripts/pg.sh up) && cargo test -p rowx-pg --test smoke`
    passes. The smoke test runs `SELECT 1` through tokio-postgres in a fresh schema.
  - With the DSN unset, the same command passes with the test skipped. With
    `ROWX_REQUIRE_DB=1` and no DSN, it fails.
  - `scripts/pg.sh down` removes the container.

### T04 · CI workflow

- **Deps:** T01, T02, T03 · **Files:** `.github/workflows/ci.yml`
- **Do:**
  - Linux job: a `postgres:18` service, `ROWX_REQUIRE_DB=1`, all of `just check`, and the
    full `cargo test` and `pytest`. Use Swatinem/rust-cache and uv cache.
  - macOS-14 job: `just check` only.
- **Accept:**
  - `actionlint .github/workflows/ci.yml` is clean.
  - The PR's own CI run is green on both jobs.

### T05 · Type-table metadata and `_core` contract

- **Deps:** T01 · **Files:** `crates/rowx-pg/src/types.rs`, `crates/rowx-py/src/types.rs`, `src/rowx/_types.py`,
  `src/rowx/_core.pyi`
- **Do:**
  - Write the `TypeInfo` struct exactly as in design §6. Fill **every** table row, with
    `Codec::Unsupported` placeholders for the codecs.
  - Export `_core.type_table()`, each entry a frozen pyclass with the same fields.
  - `_types.py` provides lookups: `by_key`, `accepts(py_type_name, key)` and
    `result_type(key)`.
  - In `_core.pyi`, declare the **planned** binding signatures from design §7.2 and §8.1:
    `connect`, `Connection._fetch`, `_execute`, `close`, `closed`, `describe`, and the
    `Describe`, `ParamInfo` and `ColumnInfo` classes. This is the contract the parallel
    lanes code against.
- **Accept:**
  - `cargo test -p rowx-pg types::` checks that keys are unique, every array row has an
    element, every supported row has a non-empty `accepted_params` (except `void`), and
    `text` accepts exactly `["str"]`.
  - `uv run python -c "from rowx import _core; t=_core.type_table(); assert {x.key for x in t} >= {'int4','numeric','timestamptz','uuid','jsonb','text','char','void'}"`
    exits 0.
  - `uv run pyright src/rowx/_core.pyi` is clean.

## P1 — Vertical slice

### T06 · `rowx-pg`: DSN, connect, close (no TLS yet), error type

- **Deps:** T01, T03 · **Files:** `crates/rowx-pg/src/{dsn,conn,error}.rs`, `crates/rowx-pg/tests/connect.rs`
- **Do:**
  - Pre-parse the DSN (design §7.1) for both URL and key=value forms. Strip the
    `ssl*` keys; for now `sslmode=disable` is the only one accepted, and the rest error
    until T23.
  - Use `NoTls`, spawn the connection task on the provided `Handle`, and make `close()`
    await it.
  - Define `Error::{Interface(String), Db{sqlstate,message,detail,hint,position,routine}, Closed, Broken}`.
- **Accept** (DB): `cargo test -p rowx-pg --test connect` covers:
  - connect, then close;
  - a bad password gives `Error::Db{sqlstate:"28P01"}`;
  - a refused port gives `Error::Interface`;
  - a query after close gives `Error::Closed`;
  - the password never appears in any error's `Display`.

### T07 · Bindings: async `connect`, `Connection`, error mapping

- **Deps:** T02, T05, T06 · **Files:** `crates/rowx-py/src/{lib,conn,errors,runtime}.rs`, `src/rowx/__init__.py`,
  `src/rowx/_query.py` (only the `Connection` wrapper), `tests/runtime/test_connect.py`
- **Do:**
  - The Python `Connection` wraps `_core.Connection`. It has `async def close`,
    `__aenter__`/`__aexit__` and `closed`.
  - `rowx.connect` is `async def`.
  - `errors.rs` is the single mapping point: it imports `rowx.errors` classes at module
    init.
  - Honour `connect_timeout`. Store `statement_cache_size`; it has no effect until T20.
- **Accept** (DB): `uv run pytest tests/runtime/test_connect.py -q` covers:
  - connect and close, `async with`, and `closed`;
  - a malformed DSN gives `InterfaceError`, and a bad password gives `DatabaseError` with
    `.sqlstate == "28P01"`;
  - `asyncio.create_task(rowx.connect(dsn))` works;
  - `inspect.iscoroutinefunction(rowx.Connection.close)` holds.

### T08 · Fetch/execute core with a minimal codec

- **Deps:** T07 · **Files:** `crates/rowx-pg/src/{exec,state}.rs` (minimal state: Idle/Busy/Closed),
  `crates/rowx-py/src/{exec,codec/mod,codec/int,codec/text}.rs`, `tests/runtime/test_fetch_core.py`
- **Do:** implement design §7.2 steps 1–6 **without** the statement cache, retries or
  parameters.
  - A non-empty `params` raises `InterfaceError("params not yet supported")`; T12 removes
    this.
  - Use the raw `FromSql`.
  - Decode `bool`, `int2`, `int4`, `int8`, `text`, `varchar` and `void`. Any other type
    raises `DecodeError`.
  - Check arity before executing, and apply the NULL mask, scalar and `limit_one`.
  - Release the GIL during IO. Move Busy → Idle before delivering the result.
- **Accept** (DB): `uv run pytest tests/runtime/test_fetch_core.py -q` covers:
  - `await c._fetch("SELECT 1, 'a'", [], [False, False], False, False, 2) == [(1, 'a')]`;
  - the scalar form returns bare values;
  - `limit_one` on `generate_series(1,100000)` returns 1 row, and the **next** call on the
    same connection succeeds;
  - an arity mismatch on `SELECT 1 WHERE false` raises `RowShapeError`, even though there
    are no rows;
  - a NULL in a non-nullable position raises `RowShapeError` mentioning index 0;
  - `_execute("CREATE TEMP TABLE x(i int)") == 0`, and
    `_execute("INSERT INTO x SELECT generate_series(1,3)") == 3`.

### T09 · `_template.py`

- **Deps:** T02 · **Files:** `src/rowx/_template.py`, `tests/unit/test_template.py`
- **Do:** implement design §3.5 in full:
  - `Compiled`, `compile_parts` and `compile_template`;
  - the adjacency rule;
  - every `TypeError` case;
  - the `:ident` rules, including the 63-byte limit;
  - nested flattening and renumbering for dynamic queries.
- **Accept:** `uv run pytest tests/unit/test_template.py -q`, table-driven, covering:
  - `t"{x}{x}"` gives `$1 $2`, and `t"SELECT 1 AS x{y}"` gives `x $1`;
  - `segments` and `placeholders` offsets are asserted for each case;
  - nested renumbering;
  - `{('s','t'):ident}` gives `"s"."t"`;
  - `{x=}`, `{x!r}` and `{x:>3}` in `query` each raise `TypeError`;
  - an ident of 64 bytes raises `TypeError`.

### T10 · `_rowplan.py` and the conformance table

- **Deps:** T02 · **Files:** `src/rowx/_rowplan.py`, `tests/conformance/row_types.py` (the table:
  `(source_text, expected_plan_or_error, expected_checker_verdict)`), `tests/unit/test_rowplan.py`
- **Do:** implement the design §3.4 grammar.
  - Unwrap aliases and `Annotated`.
  - Compute the nullable mask, with `Any` and `object` nullable.
  - Rejected forms raise `InterfaceError`. Cache plans per `T`.
- **Accept:** `uv run pytest tests/unit/test_rowplan.py -q` runs every conformance row
  through `_rowplan`. The rows include:
  - `type A = tuple[int, str | None]` gives mask `[False, True]`;
  - an alias of an alias;
  - `None` gives the no-rows plan;
  - `tuple[int, ...]`, `int | str`, `"int"` and a `TypeVar` raise `InterfaceError`;
  - `list[int | None]` is a scalar with mask `[False]`;
  - `Any` gives mask `[True]`.

### T11 · `query` / `query_unchecked` — VERTICAL SLICE

- **Deps:** T08, T09, T10 · **Files:** `src/rowx/_query.py`, `src/rowx/__init__.py`, `tests/runtime/test_query_slice.py`
- **Do:**
  - Write `_BaseQuery` and its two siblings (design §3.2), with no `__slots__`.
  - Compile eagerly in `__init__`, and build the plan lazily from `__orig_class__`.
  - `fetch_one`, `fetch_all` and `execute` are `async def`. Map the result to
    `RowNotFound` where needed.
- **Accept** (DB): `uv run pytest tests/runtime/test_query_slice.py -q` covers:
  - `await query[int](t"SELECT 1").fetch_one(conn) == 1`;
  - `query[tuple[int, str]]` gives `(1, 'a')`;
  - `fetch_all` over `generate_series(1,3)` gives `[1, 2, 3]`;
  - `WHERE false` raises `RowNotFound`;
  - `query("SELECT 1")` raises `TypeError`;
  - `query(t"SELECT 1").fetch_one(conn)` raises `InterfaceError`;
  - `query(t"SELECT 1").execute(conn)` returns `1` for a SELECT;
  - `__orig_class__` is present on instances;
  - `inspect.iscoroutinefunction(query.fetch_one)` holds.

  Tests with interpolations are `xfail` until T12.

## P2a — Codecs

### T12 · Parameter extraction and encoding infrastructure

- **Deps:** T11 · **Files:** `crates/rowx-py/src/codec/extract.rs`, `crates/rowx-pg/src/exec.rs` (encode step,
  per-parameter `encode_format`), `crates/rowx-py/src/codec/{int,text}.rs` (encode arms),
  `tests/golden/test_params_basic.py`
- **Do:** implement design §7.3: `PyParam` extraction under the GIL, then encoding without
  it.
  - Check the parameter count, giving `InterfaceError`.
  - Encode against `accepted_params` from the table. Anything else raises `EncodeError`
    naming `$n`, the Python type and the Postgres type.
  - Range-check ints. `bool` is not an `int`. `None` becomes NULL.
  - Wrap parameters in a type whose `Debug` impl is redacted.
  - Remove the "params not yet supported" error.
- **Accept** (DB): `uv run pytest tests/golden/test_params_basic.py -q` covers:
  - `query[int](t"SELECT {x}::int")` round-trips;
  - `40000` to an `int2` raises `EncodeError` mentioning `$1`;
  - `True` to an `int4` raises `EncodeError`, and `42` to a `text` raises `EncodeError`;
  - an interpolation inside a literal (`'{x}'`) raises `InterfaceError` for the count
    mismatch;
  - two interpolations bind two parameters.

  `cargo test -p rowx-pg` passes. In the PR description, report the timing of a
  release-build fetch of 100k `(int4, text)` rows, as an early de-risk on abi3
  performance.

### T13 · Numeric family

- **Deps:** T12 · **Files:** `crates/rowx-pg/src/wire/numeric.rs`, `crates/rowx-py/src/codec/{float,numeric}.rs`,
  `tests/golden/test_numeric.py`
- **Do:**
  - Cover `oid`, `float4`, `float8` and `numeric`.
  - `numeric` decodes from binary to `Decimal`, including NaN, ±Infinity, negative scale
    and trailing zeros. It encodes as text from `Decimal` or `int`.
  - `Decimal('sNaN')` raises `EncodeError`.
  - Flip the `Codec` variants in these rows only.
- **Accept:**
  - `cargo test -p rowx-pg wire::numeric`, a pure test covering `1e-30`, `1e100`, NaN and
    `-0.00`.
  - `uv run pytest tests/golden/test_numeric.py -q` covers the round-trip table, `int` →
    `numeric`, `int` → `float8`, and `float` → `numeric` raising `EncodeError`.

### T14 · Text-like, bytea, uuid, json/jsonb, enum, citext, `"char"`

- **Deps:** T12 · **Files:** `crates/rowx-py/src/codec/{text,bytes,uuid,json}.rs`, `codec/mod.rs` (Kind dispatch for
  Enum/Domain), `tests/golden/test_text_like.py`
- **Do:**
  - Dispatch through `Type::kind()` (design §6). A domain resolves to its base type.
  - jsonb decode strips the version byte. json encode always uses `json.dumps`, acquiring
    the GIL.
  - `uuid` accepts `UUID` (binary) or `str` (text).
  - bytes-likes are accepted for `bytea`.
- **Accept** (DB): `uv run pytest tests/golden/test_text_like.py -q` covers:
  - unicode, the empty string, and a bytea containing NUL;
  - jsonb `null`, `1`, `"s"`, `[…]` and `{…}`;
  - binding a `str` to jsonb stores a JSON string, and `{s}::jsonb` stores raw JSON;
  - an enum from a `str`, and citext;
  - a domain column decodes as its base type, and a domain param (`posint`) encodes;
  - `bytes` to an `int4` raises `EncodeError`.

### T15 · Temporal decode

- **Deps:** T12 · **Files:** `crates/rowx-pg/src/wire/temporal.rs`, `crates/rowx-py/src/codec/temporal.rs`,
  `tests/golden/test_temporal_decode.py`
- **Do:**
  - Decode `date`, `time`, `timetz` (negating the offset), `timestamp`, `timestamptz`
    (as UTC-aware) and `interval`.
  - ±infinity, years outside 1..9999, and intervals with months raise `DecodeError` with
    a hint.
  - Cache the Python constructors.
- **Accept:**
  - `cargo test -p rowx-pg wire::temporal`.
  - The pytest goldens cover `0001-01-01`, `9999-12-31`, `'12:00+02'::timetz` with
    utcoffset +2h, `'infinity'::date` raising `DecodeError`, `interval '1 month'` raising
    `DecodeError`, and `interval '1 day 2 hours'` as a `timedelta`.

### T16 · Temporal encode

- **Deps:** T15 · **Files:** `crates/rowx-py/src/codec/temporal_enc.rs`, `tests/golden/test_temporal_encode.py`
- **Do:** implement the design §6 rules.
  - The native pairs encode as binary; `timetz` encodes as text.
  - These raise `EncodeError`: a naive value to `timestamptz`, an aware value to
    `timestamp`, a naive `time` to `timetz`, a `datetime` to `date`, and a `str` to `date`.
- **Accept** (DB): the pytest goldens round-trip every temporal type and assert each of
  the `EncodeError` cases.

### T17 · Array decode

- **Deps:** T13, T14, T15 · **Files:** `crates/rowx-pg/src/wire/array.rs`, `crates/rowx-py/src/codec/array.rs`,
  `tests/golden/test_arrays_decode.py`
- **Do:**
  - Decode 1-D and multi-dimensional arrays into nested lists.
  - Handle empty arrays and NULL elements.
  - Ignore non-default lower bounds, and pin that behaviour with a test.
  - Element codecs resolve through `Kind`, which covers domain and enum arrays.
- **Accept:**
  - `cargo test -p rowx-pg wire::array`.
  - pytest covers `ARRAY[1,NULL,3]`, `'{{1,2},{3,4}}'::int[]`, `'{}'::text[]`, `mood[]`,
    `posint[]`, `uuid[]` and `timestamptz[]`.

### T18 · Array encode and `ANY({list})`

- **Deps:** T16, T17 · **Files:** `crates/rowx-py/src/codec/array_enc.rs`, `tests/golden/test_arrays_encode.py`
- **Do:**
  - Use binary when every element encodes as binary, writing the element OID exactly as
    declared. Otherwise build the text literal (design §7.3).
  - Accept `list` and `tuple`. Ragged nesting raises `EncodeError`.
- **Accept** (DB): pytest covers:
  - `WHERE id = ANY({[1,2,3]})`;
  - strings containing `"`, `\`, `,`, `{}`, the word `NULL`, the empty string and spaces,
    all round-tripping exactly;
  - a list of `Decimal` to `numeric[]`;
  - `[]`, `[1, None]` and `posint[]`;
  - a ragged list raising `EncodeError`.

### T19 · Codec completeness gate

- **Deps:** T13–T18 · **Files:** `tests/golden/test_table_coverage.py`
- **Do:**
  - Add a meta-test: every supported `type_table()` row has at least one decode golden and
    one encode golden, or a listed exemption.
  - No row may be left at `Codec::Unsupported` unless the table marks it unsupported.
  - `money`, `inet`, `int4range` and `aclitem` must fail cleanly: `DecodeError`, or
    `DatabaseError` 42883 for `aclitem`.
- **Accept:** `uv run pytest tests/golden -q` is green. Deleting one golden entry makes
  `test_table_coverage` fail; demonstrate this in the PR.

## P2b — Driver features, end to end, typing

### T20 · Statement cache and retry rules

- **Deps:** T11 · **Files:** `crates/rowx-pg/src/stmt_cache.rs`, `exec.rs` (lookup and retry),
  `tests/runtime/test_stmt_cache.py`
- **Do:** implement design §7.2 (schema-change retry) and §7.4.
  - The cache is an LRU per connection; `statement_cache_size=0` disables it.
  - Retry once on `0A000` with routine `RevalidateCachedQuery`. A `25P02` on the retry
    re-raises the original error with the hint. On `26000`, clear the cache and retry.
- **Accept:**
  - `cargo test -p rowx-pg stmt_cache`, a pure test of LRU order and the size-0 case.
  - DB pytest:
    - after 300 distinct queries with size 256, `pg_prepared_statements` holds ≤ 256
      rows;
    - after `ALTER TABLE … ADD COLUMN`, re-running a cached `SELECT *` succeeds;
    - the same change inside `BEGIN` raises `DatabaseError` with sqlstate `0A000` and the
      hint;
    - after `DISCARD ALL`, the next cached query succeeds.

### T21 · Connection state machine

- **Deps:** T11 · **Files:** `crates/rowx-pg/src/state.rs`, wiring in `conn.rs` and `exec.rs`,
  `tests/runtime/test_busy.py`
- **Do:** implement design §7.5 in full.
  - The states are `Idle | Busy | Settling | Broken | Closed`, held in an atomic and
    claimed by CAS in the synchronous part of the pymethod.
  - A call arriving in `Settling` awaits the notification.
  - `close()` while `Busy` cancels; until T22 lands this is a stub that waits.
- **Accept:**
  - `cargo test -p rowx-pg state::` covers the full transition table.
  - DB pytest (`timing`):
    - task A runs `pg_sleep(1)`; once a second connection sees it in `pg_stat_activity`,
      a call raises `ConnectionBusyError`;
    - after A finishes, the next call succeeds;
    - `await a(); await b()` never raises Busy, over 1000 iterations;
    - an error result leaves the state `Idle`.

### T22 · Cancellation (Settling, own CancelRequest, drain)

- **Deps:** T21 · **Files:** `crates/rowx-pg/src/cancel.rs`, `exec.rs`, `tests/runtime/test_cancel.py`
- **Do:** implement design §7.6 exactly.
  - Each operation runs as its own task, with a guard that is disarmed on every completion
    path.
  - On a drop while armed: set `Settling`, write the CancelRequest yourself and read the
    socket to EOF, drain to `ReadyForQuery`, then go `Idle`, or `Broken` on an IO error.
  - Spawn through the stored `Handle`.
  - Leave a `tls` hook so T23 can reuse its connector.
- **Accept** (DB, `timing`):
  - Cancelling `pg_sleep(3)` after 100 ms clears the backend from `pg_stat_activity`
    within 2 s, and the next `SELECT 1` succeeds.
  - **Stress:** 300 iterations of `wait_for(fetch("SELECT pg_sleep(0.004)"), random 2–6 ms)`
    immediately followed by a query. **0** follow-ups fail with 57014.
  - A plain SQL error (e.g. `SELECT 1/0`) sends **no** CancelRequest. Assert this with a
    test-only counter.
  - Cancelling a `fetch_all` of 1M rows leaves the connection usable.
  - `close()` during a long query returns within 2 s.

### T23 · TLS (rustls, sslmode mapping, sslrootcert)

- **Deps:** T06, T22 · **Files:** `crates/rowx-pg/src/tls.rs`, `dsn.rs` (ssl keys), `scripts/gen-certs.sh`,
  `scripts/pg.sh` (`up --ssl`), `tests/runtime/test_tls.py`
- **Do:** implement the design §7.1 table.
  - Use custom rustls verifiers (accept-any, CA-only and full) with the `ring` provider
    only.
  - `sslcert`, `sslkey` and `sslcrl` raise `InterfaceError`.
  - Cancel requests reuse the same connector.
- **Accept** (DB with an SSL container): `ROWX_TEST_DSN_SSL=… uv run pytest tests/runtime/test_tls.py -q`
  covers:
  - `verify-full` succeeds with the right CA;
  - the wrong CA fails;
  - `verify-full` against the wrong-host cert fails;
  - `verify-ca` against the wrong-host cert succeeds;
  - `require` succeeds without a CA;
  - `prefer` against a non-SSL server succeeds in plaintext;
  - `sslcert=…` raises `InterfaceError`;
  - cancellation works over TLS, rerunning T22's first case.

  `cargo tree -p rowx-pg | grep -c aws-lc` prints `0`.

### T24 · Benchmark

- **Deps:** T13, T14, T15 · **Files:** `bench/fetch_100k.py`, `justfile` (`bench`), `.github/workflows/bench.yml`
  (`continue-on-error: true`), `pyproject.toml` (the `bench` group)
- **Do:**
  - Use a release build and 100k rows of `(int4, text, timestamptz)`.
  - Take the median of 7 runs for rowx and for asyncpg, and print
    `rowx_ms asyncpg_ms ratio`.
- **Accept:**
  - `just bench` exits 0 and prints the three numbers.
  - The ratio is reported in the PR. If it is > 1.0, open an issue; it is not a gate.

### T25 · End-to-end example and runtime error suite

- **Deps:** T12, T13, T14, T20 · **Files:** `tests/runtime/test_example.py`, `tests/runtime/test_errors_runtime.py`
- **Do:**
  - Run the design §3.1 example against the seeded data.
  - Cover every error in design §3.7:
    - `DatabaseError` for a syntax error, with `.position`, and for a unique violation;
    - `EncodeError` and `DecodeError`;
    - `InterfaceError` for a closed connection, a count mismatch, and an absent T at fetch;
    - `RowShapeError` for arity and for NULL.
  - Cover `execute()` on `query[T]`, and `query_unchecked` with a nested template and
    `:ident`.
- **Accept:** `uv run pytest tests/runtime -q` is green, and the test names mirror the
  §3.5 and §3.7 bullets; list them in the PR.

### T26 · Type-checker suite

- **Deps:** T11 · **Files:** `tests/typing/{cases.py,test_typing.py}`
- **Do:**
  - Run `pyright --outputjson` and `mypy` as subprocesses with
    `--python-version 3.14`.
  - Parse the `reveal_type` output and compare it with the `# reveal:` comments. Cover:
    - scalars, tuples and aliases;
    - the default `None`, and `query[Any]`;
    - `fetch_all` and `execute`.
  - Negative cases: `query("…")` and `query(f"…")` must produce errors in both checkers.
- **Accept:** `uv run pytest tests/typing -q` is green, and flipping one expectation makes
  it fail.

### T27 · Runtime half of the conformance suite (complete)

- **Deps:** T10, T11 · **Files:** `tests/conformance/test_runtime.py`
- **Do:** for every conformance row with a runtime verdict, build a `query[T]` and assert
  either the plan or `InterfaceError` at the first fetch. Use a fake connection for the
  error rows and the DB for 3 representative OK rows.
- **Accept:** `uv run pytest tests/conformance -q` is green.

## P2c — Describe and nullability

### T28 · EXPLAIN fixture capture

- **Deps:** T03 · **Files:** `scripts/capture-explain.sh`, `crates/rowx-pg/tests/fixtures/plans/*.{sql,json}`
- **Do:**
  - Capture one fixture for every design §8.4 row and every §8.5 construct: anti, semi,
    UNION, recursive CTE, LATERAL, window, right and full joins, nested subquery,
    ROLLUP/GROUPING SETS, `MERGE`/`INSERT … RETURNING old/new`, and duplicate columns.
  - Capture with `EXPLAIN (GENERIC_PLAN, VERBOSE, COSTS OFF, FORMAT JSON)` through
    `docker exec psql`.
  - Record the server version in each JSON file's sidecar comment.
- **Accept:** `scripts/capture-explain.sh --check` regenerates into a temp directory and
  `diff -r` shows nothing.

### T29 · Plan walker: join marking (pure)

- **Deps:** T01, T28 · **Files:** `crates/rowx-pg/src/explain.rs`
- **Do:** implement the design §8.2.1 rules for top outputs, join marking and descending
  through every node type. Promote every matching position.
- **Accept:** `cargo test -p rowx-pg explain::join` runs with no DB. It has one test per
  join fixture, including the three sqlx-wrong rows and the duplicate-column row, plus a
  property test that the walker only ever returns promotions.

### T30 · Plan walker: scan boundaries, grouping sets, RETURNING old/new (pure)

- **Deps:** T29 · **Files:** `crates/rowx-pg/src/explain.rs` (new functions in a submodule `explain/boundaries.rs`)
- **Do:** implement the rest of design §8.2.1: Subquery Scan, CTE Scan and WorkTable Scan
  boundaries, `Grouping Sets`, and `ModifyTable` old/new prefixes, including Merge.
- **Accept:** `cargo test -p rowx-pg explain::` passes on the fixtures for the
  window-function subquery, the MATERIALIZED CTE, ROLLUP, `INSERT … RETURNING old.name`,
  `DELETE … RETURNING new.name` and MERGE.

### T31 · Describe base: prepare, params/columns, checker session, binding

- **Deps:** T07, T05 · **Files:** `crates/rowx-pg/src/describe.rs`, `crates/rowx-py/src/describe.rs`,
  `src/rowx/_core.pyi` (fill in signatures), `tests/describe/test_base.py`
- **Do:**
  - Implement design §8.1 and §8.2 step 1: the `Describe` structs, and
    `_core.describe(conn, sql)`.
  - Add `_core.connect_checker(dsn, statement_timeout)`, which applies the session
    settings from §8.1.
  - Resolve unsupported types through `typsend`.
- **Accept** (DB): `uv run pytest tests/describe/test_base.py -q` covers:
  - `SELECT id, name FROM users WHERE id = $1` gives params `[int4]` and the expected
    column types;
  - bad SQL raises `DatabaseError` with `.position`;
  - describing `INSERT INTO users …` leaves the row count unchanged;
  - `"SELECT 1; SELECT 2"` raises `DatabaseError` (the multi-statement invariant);
  - a `money` column gives `type_key` Unsupported, and `aclitem` gives Unsupported via
    typsend;
  - the enum, citext and domain columns resolve;
  - `SHOW application_name` on the checker session returns `rowx-check`.

### T32 · Base nullability from the catalog

- **Deps:** T31 · **Files:** `crates/rowx-pg/src/describe.rs` (catalog query), `tests/describe/test_base_nullability.py`
- **Do:** implement design §8.2 step 2 as one batched query, covering `relkind` and
  `convalidated`.
- **Accept** (DB):
  - `users.id` gives NotNull, `users.email` gives Nullable, and `count(*)` gives
    `Computed`;
  - the `NOT VALID` column gives `NotValidatedConstraint` (nullable);
  - a view column, and the foreign table if one is present, give `ForeignOrView`;
  - a test-only query counter shows exactly 1 catalog round trip for a 10-column query.

### T33 · EXPLAIN integration, overrides, defaulting, regression suite

- **Deps:** T30, T32 · **Files:** `crates/rowx-pg/src/describe.rs`, `tests/describe/test_nullability_regression.py`,
  `tests/describe/sqlx_cases.py` (ported, with attribution header)
- **Do:** implement design §8.2 steps 3–5.
  - Run EXPLAIN through `simple_query`, and skip it when the statement has 0 columns.
  - An EXPLAIN failure is non-fatal.
  - Strip the `!`/`?` suffixes, then default Unknown to Nullable.
- **Accept** (DB): `uv run pytest tests/describe -q`:
  - every design §8.4 row is asserted **exactly**, including the reasons;
  - `count(*) AS "n!"` gives NotNull with `OverrideBang`;
  - `SHOW work_mem` gives a Nullable column with `ExplainFailed` and no exception;
  - the ported sqlx cases pass.

### T34 · Known limits pinned

- **Deps:** T33 · **Files:** `tests/describe/test_known_limits.py`, `docs/known-limits.md`
- **Do:** write one test per design §8.5 item. Each test has a `# limit:` or `# correct:`
  marker, and `docs/known-limits.md` lists them.
- **Accept:** the pytest run is green, and a test asserts that the number of markers
  equals the number of rows in `docs/known-limits.md`.

## P2d — Checker

### T35 · Config and file walking

- **Deps:** T02 · **Files:** `src/rowx/check/{config,walk}.py`, `tests/check/test_config.py`, `tests/check/test_walk.py`
- **Do:** implement design §9.1 (config discovery and paths) and §9.2.
  - Unknown keys get a did-you-mean suggestion.
  - Parse durations.
  - Apply the default excludes, and honour `.gitignore` through `git ls-files`, falling
    back to the exclude globs.
- **Accept:** `uv run pytest tests/check/test_config.py tests/check/test_walk.py -q`,
  including an upward search for `pyproject.toml`, explicit paths still honouring
  `exclude`, and `.venv` being excluded with no `.gitignore` present.

### T36 · Discovery: lexical resolution, call shape, `indirect-query`, `syntax-error`

- **Deps:** T02 · **Files:** `src/rowx/check/{discover,model}.py` (`model.py` holds `Diagnostic`, `QueryCall` and the
  `Span` dataclasses), `tests/check/test_discover.py`
- **Do:** implement design §9.3 apart from the source map.
  - Decode files with BOM and `coding:` support.
  - Resolve imports lexically within their scopes.
  - Enforce the allowed-use list, the call-shape rules and the interpolation rules.
  - Count `query_unchecked` calls.
- **Accept:** `uv run pytest tests/check/test_discover.py -q`, one test per §9.3 bullet:
  - aliased imports, function-local imports and `if TYPE_CHECKING` imports;
  - `Q = query[int]` gives `indirect-query`, as do `partial(query, …)`, a subclass and
    `from rowx import *`;
  - a name rebound in the scope gives `indirect-query`;
  - a keyword argument, or a variable argument, gives `dynamic-query`;
  - `{x=}`, `{x!r}` and `{t"..."}` give `dynamic-query`;
  - a file with a syntax error gives `syntax-error` with its position.

### T37 · Source map

- **Deps:** T09, T36 · **Files:** `src/rowx/check/sourcemap.py`, `tests/check/test_sourcemap.py`
- **Do:** implement the design §9.3.1 algorithm exactly.
- **Accept:** `uv run pytest tests/check/test_sourcemap.py -q` covers every fixture listed
  in §9.3.1. For each sample, the reconstructed SQL equals
  `_template.compile_template(eval(sample)).sql`, run in the test. Every SQL character maps
  to a source position whose character is identical, for exact segments.

### T38 · Row-type resolution and the checker half of the conformance suite

- **Deps:** T36, T10 · **Files:** `src/rowx/check/resolve.py`, `tests/check/test_resolve.py`, `tests/check/projects/**`
  (mini project trees), `tests/conformance/test_checker.py`
- **Do:** implement design §9.4, covering the grammar, import-resolved leaves, aliases
  across modules, the module graph, and the hop limit and cycle detection.
- **Accept:** `uv run pytest tests/check/test_resolve.py tests/conformance -q`. The tests
  include:
  - a cross-module alias, a relative import, and an `__init__` re-export;
  - a cycle gives `unresolved-row-type`;
  - a generic alias gives `invalid-row-type`;
  - a fixture module containing `raise SystemExit` is never executed: `sys.modules` is
    unchanged, and the process doesn't exit;
  - every conformance row's checker verdict matches.

### T39 · Comparison engine (pure)

- **Deps:** T05, T38 · **Files:** `src/rowx/check/compare.py`, `tests/check/test_compare.py`
- **Do:**
  - Implement design §9.5 for every rule except `param-type-mismatch`, the parse-time
    rules and `sql-error`.
  - The input is a hand-built `Describe`-shaped dataclass plus a resolved `RowType`, and
    the engine uses the real `type_table()`.
  - Apply the exceptions for `Any`/`object`, JSON, arrays and `void`.
  - `arity-mismatch` suppresses the element rules.
- **Accept:** `uv run pytest tests/check/test_compare.py -q`. It has one test per rule row
  and per exception, needs no DB, and includes `bool` against `int4` giving
  `type-mismatch`.

### T40 · Diagnostic rendering

- **Deps:** T37, T39 · **Files:** `src/rowx/check/render.py`, `tests/check/test_render.py`,
  `tests/check/expected/*.{txt,json}`
- **Do:** implement the design §9.7 text and JSON formats, with element carets, alias
  notes, `NullReason` notes and help text. Diagnostics are sorted.
- **Accept:** `uv run pytest tests/check/test_render.py -q`, with byte-exact goldens for:
  - an inline element caret, and an alias note;
  - `sql-error` on a single line, across multiple lines, after `{x}`, and at end of input;
  - the inexact fallback;
  - JSON for a clean run (`[]`) and for a run with two diagnostics.

### T41 · CLI orchestration and fixture harness

- **Deps:** T31, T33, T35, T36, T37, T38, T39, T40 · **Files:** `src/rowx/check/{runner,cli}.py`,
  `tests/check/harness.py`, `tests/check/test_cli.py`, `tests/check/cases/ok_basic.py`
- **Do:** implement design §9.1 end to end:
  - checker sessions (`-j`), with describe deduplicated per SQL;
  - the `param-count-mismatch`, `missing-row-type`/`unexpected-row-type` (including the
    void rule) and `invalid-override` checks against the live describe;
  - exit codes, `--statistics` and `--output-format`;
  - reconnect once, and Ctrl-C handling.

  The harness parses `# error: [rule, …]` markers and diffs them against the actual
  diagnostics.
- **Accept** (DB):
  - `uv run rowx check tests/check/cases/ok_basic.py --database-url $ROWX_TEST_DSN` exits
    0.
  - `uv run pytest tests/check/test_cli.py -q` covers:
    - an unreachable DB exits 2, and an unknown config key exits 2;
    - a file with a nullability error exits 1;
    - `-j 1` and `-j 4` produce byte-identical output;
    - `--statistics` gives the expected counts;
    - the JSON output is valid;
    - the harness self-test: a wrong marker makes it fail;
    - the DSN password never appears in any output.

### T42 · Rule fixture suites and fidelity test

- **Deps:** T41 · **Files:** `tests/check/cases/<rule>.py` (one per §9.5 rule), `tests/check/test_cases.py`,
  `tests/check/test_fidelity.py`
- **Do:**
  - Write a case file for every rule.
  - Add a meta-test: every rule in §9.5 appears in at least one marker.
  - The fidelity test (design §10) executes every OK-case query in a rolled-back
    transaction over `seed_data.sql`, and asserts that no NULL appears in a column that
    `describe` called NotNull.
- **Accept:** `uv run pytest tests/check -q` is green. The cases include the
  `LIKE '%{q}%'` mistake (`param-count-mismatch`), `SELECT pg_sleep(0)` with no T (OK),
  `query_unchecked` (skipped and counted), and a cross-module alias.

### T43 · Parameter type inference (pure AST)

- **Deps:** T36, T38 · **Files:** `src/rowx/check/params.py`, `tests/check/test_params.py`
- **Do:** implement the design §9.6 rules exactly: literals, known constructors resolved
  through imports, the single-binding rule, and scopes.
- **Accept:** `uv run pytest tests/check/test_params.py -q`. The cases include:
  - a reassigned local, an `AugAssign`, a loop target and walrus are each unknown;
  - `bool` is not `int`;
  - `X | None` gives `X`, and `int | str` is unknown;
  - a list literal gives its element type;
  - a comprehension's scope is isolated;
  - a class-body name is invisible in a method.

### T44 · `param-type-mismatch` wiring

- **Deps:** T41, T43 · **Files:** `src/rowx/check/compare.py` (param block only),
  `tests/check/cases/param_type_mismatch.py`, `tests/check/cases/params_unknown_ok.py`
- **Do:** compare each evident type with `accepted_params` for that parameter's
  `type_key`. Report at the interpolation's span.
- **Accept** (DB): `uv run pytest tests/check -q` is green.
  - Flagged: a `str` literal for an `int4` parameter; `42` for a `text` parameter.
  - Not flagged: an unknown expression; `int` for `numeric`; `str` for `uuid`; `None` for
    anything.

## P3 — Docs and release

### T45 · User guide and generated type doc

- **Deps:** T25, T34, T42 · **Files:** `README.md`, `docs/guide.md`, `docs/types.md` (generated),
  `scripts/gen-types-doc.py`, `tests/docs/test_snippets.py`
- **Do:** cover:
  - a quickstart;
  - the writing guidance from §3.5: ANY, literal braces, no interpolation inside
    literals, casts;
  - overrides;
  - transactions on a single task;
  - the checker's safety statement from §8.1, word for word;
  - a CI recipe: GitHub Actions with a `postgres:18` service, then migrate, then
    `rowx check`;
  - poolers and `statement_cache_size`;
  - the known limits.
- **Accept:**
  - `uv run python scripts/gen-types-doc.py --check` exits 0.
  - The README and guide code blocks tagged `python test` run in
    `tests/docs/test_snippets.py` (DB) and pass.

### T46 · Example app checked in CI

- **Deps:** T44, T45 · **Files:** `examples/app/{schema.sql,main.py,pyproject.toml}`, `.github/workflows/ci.yml` (an
  `examples` job)
- **Accept** (DB):
  - `uv run python examples/app/main.py` exits 0.
  - `uv run rowx check examples/app` exits 0.
  - A copy with one broken annotation exits 1 (this lives in `tests/check/cases`).

### T47 · Wheels, release workflow, wheel smoke test

- **Deps:** T04, T46 · **Files:** `.github/workflows/release.yml`, `scripts/smoke-wheel.sh`
- **Do:** use maturin-action to build manylinux2014 x86_64 and aarch64, macOS arm64 and
  x86_64, all as cp314-abi3, and publish on tag.
- **Accept:**
  - `actionlint` is clean.
  - `scripts/smoke-wheel.sh dist/*.whl` creates a fresh venv, runs `pip install`, runs the
    example, and runs `rowx check examples/app`, exiting 0.
  - A tag dry-run (`workflow_dispatch`) builds every target.

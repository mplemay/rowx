# Architecture

```text
┌──────────────────────────── Python (rowx package) ────────────────────────────┐
│  SHARED (never imports rowx._core; `import rowx` loads no backend)            │
│  rowx/__init__.py, _query.py   query, query_unchecked, QueryResult, RowStream │
│  rowx/_executor.py             Executor protocol (private SPI)                │
│  rowx/errors.py                exception hierarchy (Errors)                   │
│  rowx/_template.py             compile_parts / compile_template               │
│  rowx/_rowplan.py              T → RowPlan (Row-type grammar)                 │
│  POSTGRES                                                                     │
│  rowx/postgres/                connect, ConnectOptions, Connection, Pool,     │
│                                Transaction, RESET_QUERY, _types.py, _vocab.py │
│  rowx/check/                   checker front-end (stdlib ast), CLI; Postgres  │
└──────────────────────────────────┬────────────────────────────────────────────┘
                                   │ PyO3 0.28 (abi3-py314)
                                   │ rowx._core  +  rowx._core.postgres
┌──────────────────────────────────▼────────────────────────────────────────────┐
│  crates/rowx-py            cdylib `_core`: registers the shared functions and │
│                            the `rowx._core.postgres` submodule                │
│  crates/rowx-postgres-py   rlib: codec/, conn, exec, stream, pool,            │
│                            transaction, describe, types, errors.rs (Postgres  │
│                            error mapping)                                     │
│  crates/rowx-py-core       rlib: the runtime, the exception-class registry    │
│                            (errors.rs), the core pool-error mapping           │
│  crates/rowx-postgres      PURE RUST (no pyo3): dsn, tls, conn,               │
│                            cancel (impl), stmt_cache, exec, describe,         │
│                            explain,                                           │
│                            types.rs, wire/, and its pool connector            │
│  crates/rowx-core          PURE RUST (no pyo3): claim/settle state machine,   │
│                            cancel-guard plumbing, shared thiserror variants,  │
│                            the pool                                           │
└──────────────────────────────────┬────────────────────────────────────────────┘
                                   │ tokio-postgres 0.7.x on a multi-thread tokio runtime
                                   ▼
                              PostgreSQL 18
```

Decisions:

- **`rowx-core` and `rowx-postgres` are pure Rust** (no pyo3), so `cargo test` is real.
  - `rowx-core` holds what any backend shares: the claim/settle state machine, the
    cancel-guard plumbing, the shared `thiserror` variants and the pool.
  - `rowx-postgres` holds what is Postgres-specific: connections, DSN and TLS, the statement
    cache, describe, the EXPLAIN walker, the type-table metadata, the byte-level wire codecs
    (which return plain Rust values, such as numeric digits and microseconds) and the pool's
    connector.
- **The backend trait.** `Cancel` held Postgres types. `rowx-core` now defines a small trait
  (send a cancel request, an is-alive check, with `Send` futures). `rowx-postgres`
  implements it, and the pool's `Manage` trait reuses it.
- **`rowx-py-core` and `rowx-postgres-py` own all PyObject work.**
  - `rowx-py-core` holds the runtime and the exception-class registry.
  - `rowx-postgres-py` extracts Python values into a neutral Rust enum
    ([Codec](driver.md#codec-rowx-postgres-pysrccodec-rowx-postgressrcwire)), builds Python objects from wire values,
    and maps the Postgres errors.
  - `rowx-py` is the only cdylib. It registers the shared functions in `rowx._core` and the
    Postgres bindings in the `rowx._core.postgres` submodule.
- **A second backend only adds crates and a namespace** (for example `rowx-sqlite`,
  `rowx-sqlite-py` and `rowx.sqlite`). No other backend is implemented.
- **Error mapping.** The shared exception-class registry is in `rowx-py-core/src/errors.rs`.
  Each backend maps its own `thiserror` enum in its own `errors.rs`
  (`rowx-postgres-py/src/errors.rs`). The core pool errors (`PoolTimeout`, `PoolClosed`) are
  mapped once, in `rowx-py-core`.
- **`rowx._core` and `rowx._core.postgres`.**
  - `rowx._core` keeps the shared bindings. The Postgres bindings live in
    `rowx._core.postgres`, a PyO3 submodule that is registered in `sys.modules` at init, so
    `from rowx._core.postgres import …` works.
  - Postgres pyclasses use `module = "rowx._core.postgres"`. The Postgres-only test hooks
    (`_cancel_requests_sent`, `_catalog_queries`) live in the submodule.
  - `_core.pyi` is a stub package: `_core/__init__.pyi` and `_core/postgres.pyi`.
- **Python module map.**
  - Shared, in `rowx`: `__init__.py`, `_query.py`, `_template.py`, `_rowplan.py`,
    `_executor.py` and `errors.py`. None of them imports the extension, so `import rowx`
    loads no backend, and `rowx` never imports `rowx.postgres`.
  - Postgres, in `rowx.postgres`: the public surface ([API](api.md#public-surface)),
    plus `_types.py` (the typed view of `type_table()`) and `_vocab.py`, which are
    Postgres-specific.
  - The checker stays in `rowx.check` and imports Postgres internals. It is Postgres-only,
    and backend dispatch in the checker is deferred.
- **The checker front-end is Python** and uses stdlib `ast` and `tokenize`. It gets exact
  PEP 750 support and spans, and it can read type aliases from source without importing
  user code. All database work goes through `rowx._core.postgres`, over the same connection
  type the runtime uses.
- `rowx._core` and `rowx._core.postgres` **MUST** import, and `type_table()` **MUST** work,
  with no database. The checker's AST-only phases depend on this.
- Free-threading is out of scope: `abi3-py314` rules out `3.14t`.

## Packaging and toolchain

- There is one distribution, `rowx`. It is built with maturin and abi3-py314, with
  `requires-python = ">=3.14"`. Wheels target cp314-abi3: macOS arm64 and x86_64, and Linux
  manylinux x86_64 and aarch64. Windows is best effort, and `3.14t` is unsupported.
- Console script: `rowx = "rowx.check.cli:main"`.
- **Workspace layout.** A root `Cargo.toml` defines the workspace, with members
  `crates/rowx-core`, `crates/rowx-postgres`, `crates/rowx-py-core`,
  `crates/rowx-postgres-py` and `crates/rowx-py`. Only `rowx-py` has `lib.name = "_core"`
  and `crate-type = ["cdylib"]`; `rowx-py-core` and `rowx-postgres-py` are rlibs.
- **Maturin.** `[tool.maturin]` sets `manifest-path = "crates/rowx-py/Cargo.toml"`,
  `module-name = "rowx._core"`, `python-source = "src"`, and
  `features = ["pyo3/extension-module"]`. `extension-module` must **not** be hard-coded in
  `Cargo.toml`, because that breaks `cargo test` and `cargo clippy` linking.
- **uv cache keys.** `[tool.uv] cache-keys` must list `pyproject.toml`,
  `crates/**/*.rs`, `crates/**/Cargo.toml`, `Cargo.toml`, `Cargo.lock` and
  `rust-toolchain.toml`. Otherwise `uv run` silently tests a stale build.
- **Rust toolchain.** `rust-toolchain.toml` pins an exact stable release (≥ 1.85 for
  edition 2024) with `rustfmt` and `clippy`. `Cargo.lock` is committed.
- **Rust dependencies**, pinned to exact minor versions:
  - `tokio-postgres` 0.7.x, keeping its default `runtime` feature;
  - `tokio` with `rt-multi-thread`;
  - `pyo3` 0.28 with `pyo3-async-runtimes` 0.28 (tokio). These two minors always move
    together. 0.29 exists, and the upgrade is a separate task.
  - `tokio-postgres-rustls`, plus `rustls` with `default-features = false` and
    `features = ["ring","std","tls12"]`. There must be exactly one `CryptoProvider`.
    Linking two makes `ClientConfig::builder()` panic, and aws-lc-sys complicates
    manylinux-aarch64 builds.
  - `webpki-roots`, `rustls-pemfile`, `serde_json`, `lru`, `bytes`, `futures-util` and
    `thiserror`.
  - **Not** `sqlx` and **not** `sqlparser` ([Dependency evaluation](dependencies.md#dependency-evaluation)).
- **Python** has no runtime dependencies. The dev group holds `pytest`, `pytest-asyncio`
  (with `asyncio_mode = "auto"`), `pyright` and `mypy` (both version-pinned), and `ruff`.
  An optional `bench` group holds `asyncpg`.
- **Builds.** `maturin develop` and `uv run` produce **debug** builds, which are fine for
  tests. Benchmarks **MUST** use `maturin develop --release`. Never run a plain
  `cargo build` of the cdylib, because it doesn't link on macOS.
- **Datetime C-API.** abi3 doesn't expose it. Temporal decoding calls the Python
  constructors, cached with `PyOnceLock`. The spike's numbers were measured this way.

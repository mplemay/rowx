# Architecture

```text
┌──────────────────────────── Python (rowx package) ────────────────────────────┐
│  rowx/__init__.py, _query.py   query, query_unchecked, connect, Connection    │
│  rowx/errors.py                exception hierarchy (Errors)                     │
│  rowx/_template.py             compile_parts / compile_template (shared)      │
│  rowx/_rowplan.py              T → RowPlan (grammar Row-type grammar and runtime row plan)                     │
│  rowx/_types.py                typed view of _core.type_table() (shared)      │
│  rowx/check/                   checker front-end (stdlib ast), CLI            │
└──────────────────────────────────┬────────────────────────────────────────────┘
                                   │ PyO3 0.28 (abi3-py314)   rowx._core
┌──────────────────────────────────▼────────────────────────────────────────────┐
│  crates/rowx-py   bindings + ALL PyObject conversion                          │
│     conn.rs, exec.rs, describe.rs, errors.rs (single exception-mapping point) │
│     codec/{mod,extract,int,float,numeric,text,bytes,uuid,json,temporal,array} │
│  crates/rowx-pg   PURE RUST (no pyo3): testable with plain `cargo test`       │
│     dsn.rs, tls.rs, conn.rs, state.rs, cancel.rs, stmt_cache.rs, exec.rs      │
│     types.rs  (THE type table metadata)  wire/{numeric,temporal,array,...}.rs │
│     describe.rs, explain.rs                                                   │
└──────────────────────────────────┬────────────────────────────────────────────┘
                                   │ tokio-postgres 0.7.x on a multi-thread tokio runtime
                                   ▼
                              PostgreSQL 18
```

Decisions:

- **`rowx-pg` is pure Rust.** It owns connections, the state machine, cancellation, the
  statement cache, TLS, describe, the EXPLAIN walker, the type-table metadata, and the
  byte-level wire codecs. Those return plain Rust values, such as numeric digits and
  microseconds.
- **`rowx-py` owns all PyObject work.** It extracts Python values into a neutral Rust enum
  ([Codec](driver.md#codec-rowx-pysrccodec-rowx-pgsrcwire)), builds Python objects from wire values, and maps errors in
  one place.
- **The checker front-end is Python** and uses stdlib `ast` and `tokenize`. It gets exact
  PEP 750 support and spans, and it can read type aliases from source without importing
  user code. All database work goes through `rowx._core`, over the same connection type the
  runtime uses.
- `rowx._core` **MUST** import, and `type_table()` **MUST** work, with no database. The
  checker's AST-only phases depend on this.

## Packaging and toolchain

- There is one distribution, `rowx`. It is built with maturin and abi3-py314, with
  `requires-python = ">=3.14"`. Wheels target cp314-abi3: macOS arm64 and x86_64, and Linux
  manylinux x86_64 and aarch64. Windows is best effort, and `3.14t` is unsupported.
- Console script: `rowx = "rowx.check.cli:main"`.
- **Workspace layout.** A root `Cargo.toml` defines the workspace, with members
  `crates/rowx-pg` and `crates/rowx-py`. The `rowx-py` crate has `lib.name = "_core"` and
  `crate-type = ["cdylib"]`.
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

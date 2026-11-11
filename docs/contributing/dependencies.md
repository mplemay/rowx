# Dependency evaluation

| Candidate | Use | Verdict |
|---|---|---|
| **sqlx** as the runtime driver | Connections, codecs | **No.** Parameters are binary-only, so every Python type and Postgres type pair needs a hand-written encoder, and `int` to `numeric` and `str` to `date` failed. Its connection serialises everything. It has no public cancel API. It pulls in far more crates than tokio-postgres. |
| **sqlx** `describe` | Nullability | **No.** It is hidden, offline-gated and only reachable through macros, and its signature changed in 0.9. It gives unsafe answers ([Evidence](../reference/nullability.md#evidence)). rowx copies its model and ports its test cases with attribution. sqlx is MIT or Apache-2.0. |
| **tokio-postgres** | Runtime and describe | **Yes.** It has a public describe API, raw `FromSql`, a per-parameter format, `Kind` resolution and cancel tokens. |
| **deadpool-postgres** | Pool | **No.** Its statement cache conflicts with rowx's cache and its cancel path. The pool spike rejected `deadpool` itself as well: rowx's pool is smaller and adds no dependency. |
| **datafusion-sqlparser-rs** | Client-side parsing | **Not in this version.** The server already provides validity, types, parameter inference and outer-join nullability, and t-strings give placeholder positions. A parser would add dialect gaps. Revisit for NOT NULL narrowing beyond the [computed-column whitelist](../reference/nullability.md#computed-columns), for detecting the statement kind before connecting, and for finer mapping across [scan boundaries](../reference/nullability.md#scan-boundary-mapping). That narrowing is [#303](https://github.com/mplemay/rowx/issues/303). |
| **sqlty** | Reference | MIT, borrowed with attribution for parameter rules and config ergonomics. Not a dependency. |
| **ruff_python_parser** | A checker in Rust | **No.** Its API is unstable, and stdlib `ast` and `tokenize` are exact for 3.14. |
| **sha2** | Migration checksums | **Yes.** Checksums are SHA-384 over the raw file bytes, to match sqlx. `sha2` is pure Rust, MIT or Apache-2.0, and sqlx uses it for the same job. It lives in `rowx-core`. |
| **crc32fast** | Migration lock key | **Yes.** The advisory-lock key is sqlx's `0x3d32ad9e * crc32(current_database())`, so a sqlx migrator and a rowx migrator exclude each other. Only CRC-32/ISO-HDLC is required. It lives in `rowx-postgres`. |
| **pydantic** (>= 2.11) | Named row tests (dev group) | **Yes, dev only.** Plans, the validator call, aliases and `ValidationError` as the cause of `RowConstructError` are tested against a real install. rowx never imports it. Classes are detected through `sys.modules`, and the checker reads them from the AST. 2.11 is the floor because `validate_by_name` and `validate_by_alias` arrived there. It is not a dependency of `rowx` or of any extra. |
| **attrs**, **msgspec** | Named row types | **Not in this version.** Both would need their own detection and field rules. They are part of [#297](https://github.com/mplemay/rowx/issues/297). |
| **pytest-asyncio** | Plugin loop-scope tests (dev group) | **Yes, dev only.** `rowx.testing` must work from async tests. It is not a dependency of `rowx` or of `rowx[pytest]`. |
| **pytest-xdist** | Plugin concurrency tests (dev group) | **Yes, dev only.** A session-scoped template must be built once under `-n`. It is not a dependency of `rowx` or of `rowx[pytest]`. |

The lint gate is [Testing](testing.md). `ty` runs in `prek`. pyright and mypy are dev dependencies because the
conformance reveal test invokes them. They are not runtime dependencies.

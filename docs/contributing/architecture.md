# Architecture

The reference pages are the user contract. This page is the current tree: where the code lives, how a backend is
linked, and which checks run without a database.

## Distributions

| Distribution | Import root | Runtime dependencies |
|---|---|---|
| `rowx-core` | `rowx_core` | none |
| `rowx-postgres` | `rowx_postgres` | matching `rowx-core` only |
| `rowx` | `rowx` | matching `rowx-core`; the `postgres` extra adds matching `rowx-postgres`, and the `embedded` extra adds matching `rowx-postgres` and `rowx-embedded` |
| `rowx-embedded` | `rowx_embedded` | none; it is the optional embedded engine and does not depend on any other rowx distribution |

`from rowx import query` and `from rowx_core import query` are the same object. `import rowx` loads no backend.
Connections are `from rowx_postgres import Pool, connect`. The `postgres` extra is what installs that package. The
backend does not depend on the CLI.

Python packages:

| Path | Role |
|---|---|
| `packages/rowx-core/src/rowx_core/` | Shared query, template capture, executor protocols, errors, migration contracts. |
| `packages/rowx-postgres/src/rowx_postgres/` | PostgreSQL connection, pool, compiler, value types, `Migrator`, bound services. |
| `src/rowx/` | CLI, checker, pytest plugin, and the core re-exports. |

Rust crates:

| Path | Role |
|---|---|
| `crates/rowx-core` | No PyO3. Claim/settle (`state.rs`), cancel plumbing (`cancel.rs`), row shape (`row_shape.rs`), pool, migration plan. |
| `crates/rowx-postgres` | No PyO3. DSN, TLS, connection, execution, statement cache, describe, explain, wire codecs. Depends only on `rowx-core` among the workspace crates. |
| `crates/rowx-py-core` | Shared PyO3 support: the tokio runtime (`runtime.rs`) and the exception-class registry (`errors.rs`). |
| `packages/rowx-postgres` | The cdylib. Cargo package name `rowx-postgres-py`, library name `_core`. This is the binding crate, not a separate distribution. |

The default workspace members are those four paths. The embedded companion (`packages/rowx-embedded` and its
`crates/rowx-embedded-host`) is a member too, but not a default one (see [Embedded companion](#embedded-companion)).
`pyo3` and `pyo3-async-runtimes` are workspace dependencies and their minor versions move together. `uv sync` installs
the workspace. `ty` checks each package in place, with no merged type-check tree and no cross-package symlink. Build the
extension with `uv run maturin develop -m packages/rowx-postgres/Cargo.toml`. Do not `cargo build` the cdylib on its
own.

## Native module and runtimes

The Python module is `rowx_postgres._core` (`module-name` in `packages/rowx-postgres/pyproject.toml`).
`packages/rowx-postgres/src/lib.rs` registers it and calls `rowx_py_core::runtime::init()` from the module
initialiser. That builds one multi-thread tokio runtime for the extension, named `rowx-io`, and registers it with
`pyo3-async-runtimes`. The runtime static lives in the extension, so each backend extension links
`rowx-py-core` statically and owns its runtime. Only the PostgreSQL extension exists today.

The GIL is released around IO. IO runs on that runtime.

## Acyclic dependencies

`rowx-postgres` does not import `rowx`. The checker in `rowx` selects a backend from the database URL and receives
the backend's checker, admin and migration services as arguments. `Backend` in
`packages/rowx-core/src/rowx_core/backend.py` has `checker`, `admin`, `migrations`, and `testing` factories. A
missing one raises `UnsupportedCapabilityError`. A checker-only backend need not implement administration or
testing. There is no `ContextVar`, no default backend, and no cast through `Any` to hide a backend type. A
capability the process did not install is an error from `backend_for`, not a silent PostgreSQL fallback. Keyword
and `postgres`/`postgresql` URLs ask for `rowx_postgres`. Error redaction is string processing over the URL the
command already holds. It does not import a provider.

## Validation timing

Two steps, on purpose.

- Capturing a t-string is backend-independent and runs in the `query` constructor (`rowx_core`). It rejects a
  non-template, a NUL in static text, a conversion and an unsupported format spec. Nested templates stay a DAG.
  Expansion limits belong to the dialect.
- Emitting SQL is dialect-dependent and runs when a method is called, using the compiler on the executor passed in.
  The PostgreSQL compiler (`rowx_postgres._compiler`) emits `$n`, inserts adjacency spaces, and rejects more than
  65535 parameters. A later call on another executor compiles again. The checker does not infer which executor that
  will be. It checks the construction site, including a query nothing calls, and it checks both import roots.
- The checker's source map calls that same compiler's `compile_parts` and uses the padding it returns. Runtime SQL
  and checker spans cannot drift apart by inventing a second spacing rule.

Describe against Postgres is dialect-dependent and needs a database URL. It is skipped for `query_unchecked` SQL.
The row type is still resolved. Migration filenames, checksums and the plan are backend-independent and run in
`rowx_core` / `crates/rowx-core/src/migrate/` with no connection. Applying a migration is dialect-dependent and goes
through `rowx_postgres`.

## Drift and migrate add

The drift gate reads history through `migrator.open(readonly=True)`. That connection is the checker's read-only
session. The read takes no advisory lock and creates nothing.

`rowx migrate add` calls the shared source discovery in `rowx_core`. It does not call `backend_for`. An existing
migration directory is not a reason to load a backend. `run`, `revert`, `skip` and `info` do load one.

`rowx database reset` and `setup` construct the `Migrator`, which validates the files, before they create, drop or
change a database. A bad directory fails before the destructive step. `drop` alone does not read the files. Only
opening a connection is retried. The migration command runs once.

## Embedded companion

The embedded database (`rowx[embedded]`, [reference](../reference/embedded.md)) is a companion distribution, not a
member of the workspace. This section says how it is built and linked.

### The companion distribution

| Piece | Where | What it is |
|---|---|---|
| `rowx-embedded` | `packages/rowx-embedded/` | The distribution. A per-platform wheel built by maturin, with **static metadata** (no `dynamic` fields) so that the `uv-lock` hook never runs maturin or builds Wasmer. No sdist. |
| `rowx_embedded` | `src/rowx_embedded/` | The Python package. `__init__.py` exports `Database`, `Instance`, `ENGINE_VERSION`, `RUNTIME_VERSION`, `WASM_SHA256`, `SNAPSHOT_FORMAT`, `EXTENSIONS` and the errors `EngineError`, `DirectoryInUseError` and `SnapshotError`; `_engine.pyi` types the cdylib and `_api.py` holds the exit guard. |
| `rowx_embedded._engine` | `src/*.rs` | The pyo3 cdylib (abi3-py314): the Python surface only. |
| `rowx-embedded-host` | `crates/rowx-embedded-host/` | The host, with no pyo3: the engine thread, the front proxy, the slot state machine, snapshots and ownership. Its logic is unit-tested as pure functions. |
| the glue | `src/rowx_embedded/` | Pure Python on top of `rowx_postgres`: `_database.py` (the public `Database`, a `Pool` subclass that starts its instance lazily), `_instance.py` (the URL grammar, instance ownership and the socket DSN helpers), `_scratch.py` (the embedded service and the scratch databases for the checker), `_testing.py` (the pytest plugin's snapshot provisioner) and `_backend.py` (the backend for the `embedded` scheme). |
| `rowx.embedded` | `src/rowx/embedded.py` | The facade that re-exports `Database` and explains the missing extra. |

`rowx-embedded` depends on the matching `rowx-core` and `rowx-postgres`, never the other way round:
`rowx-postgres` and `rowx-core` know nothing about the engine, and `import rowx_postgres` never imports it. The
`embedded` URL scheme is registered in `rowx._backend` against `rowx_embedded._backend`, whose `load()` is the
PostgreSQL backend (`dataclasses.replace`) with the embedded provisioner and embedded service in place of the server
ones. The only generic seams that `rowx-postgres` and `rowx-core` keep for it are the protected hooks of `Pool`
(`_build`, `_native`, `_inner`, `_started` and `_via_python`, which a subclass uses to build its native pool after
construction), `Provisioner.identity`, the `EmbeddedService` and `ScratchService` protocols of `rowx_core.backend`, and
the driver's connect timeout. Nothing in the repository's Rust crates changed for the engine: it is reached over a Unix
socket, which the existing `Host::Unix` path of the driver already handles, and it never shares native objects with the
driver's extension.

The engine is imported directly, so a wheel whose engine cannot be loaded fails the import of `rowx_embedded` with the
cause. The distributions are pinned to one version (`rowx-embedded` requires `rowx-postgres==<version>`), so there is no
version handshake. The engine's threads exist only in the process that started an instance, and an instance is refused
in a forked child.

### Build isolation

The companion is a **workspace member that is not a default member** (`default-members` in the root `Cargo.toml`). The
engine pulls in about 440 Wasmer crates (edition 2024, rust-version 1.93) and a vendored patch (`[patch.crates-io]` in
the root manifest), all in the one root `Cargo.lock`, `target/` and lint table. Plain `cargo test`, `cargo clippy` and
`cargo tree` select only the default members, so they never build Wasmer; `maturin develop` of the other packages builds
only its own package. `--workspace` selects every member, so the everyday gates never pass it. Select the companion with
`-p rowx-embedded -p rowx-embedded-host`.

The Python side is one root lock: `rowx-embedded` is a uv workspace member in the `dev` group, so every `uv sync`
installs it (a maturin build of the engine, cached until its sources change), locally and in CI alike. Users get it
through the `embedded` extra. Its version is the lockstep release version, and `scripts/validate-release-versions.py`
checks its pyproject, both Cargo manifests and their entries in the root lock.

| Gate | What it does |
|---|---|
| `just check` (prek) | Formats the companion (`cargo fmt --all`) and type-checks `packages/rowx-embedded/src`. Its clippy runs over the default members and never builds Wasmer. |
| `just check-embedded` | Clippy for the companion: the manual-stage hook `cargo-clippy-embedded`. |
| `just test-embedded` | The embedded lane: `ROWX_TEST_DSN=embedded:// uv run pytest -m "(db and not server) or embedded"`. |

### Threads and runtime ownership

- Each `Instance` owns **one dedicated OS thread** that holds the engine handle (it is not `Send`) and serves commands
  over a channel. The engine never runs on a Python, tokio or proxy thread.
- The front proxy is an accept thread plus one thread per connection. They are plain threads, not tokio tasks.
- Every call from Python releases the GIL. The shared tokio runtime of `rowx-py-core` is untouched: it drives the
  driver's client, which talks to the proxy like any server.
- Start, close and snapshot run in a thread of their own on the Python side, not in the event loop's executor, so that
  an interrupted run never waits for an engine call that cannot be cancelled. A cancelled start finishes in its thread
  and closes the instance nobody owns.
- Instances are closed at exit (`atexit`), when the Python object dies (without blocking), and never by a forked child.

### The front proxy

The engine's own endpoint serves clients serially, so a second client and every `CancelRequest` would wait in its listen
backlog, and the driver's 5 s cancel guard would mark the connection broken. `rowx-embedded` puts its own accept thread
in front:

- The public socket is `<private dir>/.s.PGSQL.5432`. The engine itself is driven in process, with no endpoint.
- `SSLRequest` is answered `N`. A `CancelRequest` is read and its socket closed at once, so the client's settle loop
  sees EOF and drains.
- Only whitelisted frontend messages are forwarded; `COPY ... FROM STDIN` is held back until its data is complete (up to
  1 GiB, then `53200`).
- Between sessions the proxy resets the backend (`ROLLBACK`, then `DISCARD ALL`) and verifies the result. A backend that
  cannot be put back to idle is unusable and says so.

The proxy never multiplexes, because session state and tokio-postgres's per-client statement names (`s0`, `s1`, ...)
would collide. See [Driver](../reference/embedded.md#driver).

## Deferred

These items are not specified in the reference and are tracked by the issues below.

- [#296](https://github.com/mplemay/rowx/issues/296) checker suppressions and rule configuration
- [#297](https://github.com/mplemay/rowx/issues/297) remaining named row types
- [#298](https://github.com/mplemay/rowx/issues/298) opt-in query pipelining
- [#299](https://github.com/mplemay/rowx/issues/299) offline query cache
- [#300](https://github.com/mplemay/rowx/issues/300) multi-statement execution and command tags
- [#301](https://github.com/mplemay/rowx/issues/301) checker `--fix`
- [#302](https://github.com/mplemay/rowx/issues/302) column type overrides and custom codecs
- [#303](https://github.com/mplemay/rowx/issues/303) parser-based nullability narrowing
- [#304](https://github.com/mplemay/rowx/issues/304) listen/notify, copy, client certificates, libpq env and
  free-threading
- [#305](https://github.com/mplemay/rowx/issues/305) additional database backends
- [#306](https://github.com/mplemay/rowx/issues/306) package-resource migrations
- [#307](https://github.com/mplemay/rowx/issues/307) per-test SQL fixtures for `rowx.testing`

Embedded Postgres is [#194](https://github.com/mplemay/rowx/issues/194).

# Agent Instructions

## Tooling

- **`uv`:** The python package manager.
  - *Usage:* `uv`
  - *Rules:*
    - **Always run `uv` with elevated permissions.**
    - **Don't use other package managers for python dependencies (ex: `pip`, `poetry`, etc.)**
- **`cargo`**: The rust build tool (the `_core` extension is built with `maturin` via `uv`)
  - *Files:* [workspace](Cargo.toml), [toolchain](rust-toolchain.toml), [rustfmt](rustfmt.toml)
  - *Rules:*
    - **Don't use `unsafe`, `unwrap`, `expect`, or `panic!`** (denied by the lint config).
- **`prek`**: Checks, linting, and formatting
  - *Usage:* `uv run prek`
  - *Files:* [configuration](prek.toml), [shellcheck](.shellcheckrc), [shfmt](.editorconfig)
  - *Tools:* `rumdl` (markdown), `ruff` (python linting / formatting), `ty` (type checker), `shellcheck` /
    `shfmt` (shell), `cargo fmt` / `cargo clippy` (rust)
  - *Rules:*
    - **Don't run the underlying linters / formatters directly.**
    - **Never use file-wide ignores.**
    - **Never update the linter / formatter configs without explicit permission.**
- **`git`**: Version control
  - *Usage:* `git`
  - *Files:* [ignores (project)](.gitignore)
  - *Rules:*
    - **Always use all lowercase single-line conventional commit messages (ex: `feat: add query builder`).**

## Conventions

### Python

- The project targets python 3.14 or greater
- Given the project targets a more modern python, use functionality such as:
  - The walrus operator (`:=`)
  - Modern type hints (`dict`)
  - Type parameters `class MyClass[T: MyParent]: ...`
  - The `Self` type for return types (`from typing import Self`)
- Type annotations:
  - **Do not** annotate `self` parameters - the type is implicit
  - Use `Self` for return types when returning the instance
  - Example: `def add_item(self, item: str) -> Self: ...` (note: no type on `self`)
- Classes and data structures:
  - Use `@dataclass` (from `dataclasses`) instead of manually defining `__init__` for data-holding classes
  - Consider using `slots=True` for memory efficiency and attribute access protection
  - Use `kw_only=True` to require keyword arguments for better readability at call sites
  - Use `frozen=True` for immutable data structures
  - Example: `@dataclass(slots=True, kw_only=True, frozen=True)`
  - **When NOT to use dataclass**:
    - Inheriting from non-dataclass parents (can cause MRO and initialization issues)
    - Need for `__new__` method (for singleton patterns, custom object creation)
    - Complex property logic with getters/setters that transform data
    - Need for `__init_subclass__` or metaclass customization
    - Classes with significant behavior/methods (prefer traditional classes for these)
  - **When to use dataclass**:
    - Simple data containers with minimal logic
    - Configuration objects, DTOs (Data Transfer Objects), result types
    - Immutable value objects (use `frozen=True`)
    - When you want automatic `__eq__`, `__repr__`, `__hash__` implementations
- Prefer importing using `from x import y` instead of `import x`
- Import local modules using the full path (ex: `from rowx.my_module import MyClass`)
- Internal compiled module (`._core`) imports:
  - Prefer direct `from rowx._core import ...` when there is no name clash in that file.
  - When a symbol from `_core` would clash with a Python-defined name in the same module, import with a `*Impl` /
    `*_impl` alias, then assign or wrap as needed:
    - Types/classes: `FooImpl` (e.g. `from rowx._core import Foo as FooImpl`, then `Foo = FooImpl` or a thin wrapper).
    - Functions: `foo_impl` (snake_case with `_impl` suffix).
  - Do not use leading-underscore import aliases (`_Foo`, `_foo`) for this re-export pattern.
  - Keep [`_core/__init__.pyi`](src/rowx/_core/__init__.pyi) and [`_core/postgres.pyi`](src/rowx/_core/postgres.pyi) in
    sync with the rust modules.
- **Don't use** docstrings, instead add inline comments only in places where there is complex or easily breakable logic
- **No file-wide suppressions** in source: do not use a first-line or module-wide pragma such as `# ruff: noqa: ...` for
  the whole file, a blanket `# type: ignore` on a module, or equivalent file-scoped ignores.
- **Prefer fixing the cause**: adjust types or public API, or tooling configuration that matches documented conventions
  (for example `pyproject.toml`), so the diagnostic does not apply.
- **If suppression is unavoidable**, use the **smallest scope** (usually a single line) with **explicit rule codes**
  (for example `# noqa: ARG002`), not a whole-file waiver. This refers to pragmas in `.py` files, not to path-based
  rules in `pyproject.toml` (which should stay minimal and justified).
- For type aliases, prefer Python's modern syntax: `type MyAlias = SomeType` (PEP 695 style), especially in new code.
- Constants:
  - Module-level runtime constants must be public (no leading underscore), `SCREAMING_SNAKE_CASE`, and annotated with
    `Final[T]` from `typing`. This includes path-derived constants and multiline string literals, not just simple
    literals.
  - Examples:
    - `DEFAULT_HOST: Final[str] = "127.0.0.1"`
    - `PACKAGE_DIR: Final[Path] = Path(__file__).resolve().parent`
  - Does not apply to type aliases (`type Foo = ...`), TypedDict assignments, class instance attributes (including those
    annotated with `Final` in `__init__`), or special dunders (`__all__`, etc.).
- URL construction:
  - Use `urllib.parse` methods for URL manipulation (don't use string concatenation or f-strings for query params)
  - Use `urlencode()` for query parameters
  - Use `urlparse()` and `urlunparse()` for URL composition

### Rust

- Lints are configured in [`Cargo.toml`](Cargo.toml) (`clippy::all`, `pedantic`, and `nursery` are denied); fix the
  diagnostic rather than adding `#[allow(...)]`.
- If an `#[allow(...)]` is unavoidable, scope it to the smallest item with a short justification comment. Never use
  crate-wide `#![allow(...)]`.
- Return `Result` and propagate errors (convert to Python exceptions at the PyO3 boundary) instead of panicking.

### Testing

- Test files are named `test_<module>.py` to match the source module they test (e.g. tests for `core.py` go in
  `test_core.py`, tests for `_core` go in `test__core.py`)
- Do not name test files by functionality
- Tests live under `src/rowx/__tests__/` with `unit/` and `integration/` subdirectories. Group tests by domain in
  subfolders.
- Integration tests are marked with `@pytest.mark.integration`

## Project

rowx is typesafe raw SQL for Python 3.14+ with no ORM. Queries are PEP 750 t-strings with an explicit row type,
`query[tuple[int, str]](t"...")`, run by a Rust driver built on tokio-postgres. A static checker, `rowx check`,
validates every query against a live Postgres 18 database by preparing and describing it.

- [Design](docs/design/README.md) is the contract. Never edit it in a feature PR.
- If the spec is ambiguous, don't guess silently: raise it in the PR description and propose a design change.

## Commands (`just`)

| Command | What it does |
|---|---|
| `just check` | All gates through `uv run prek run --all-files` |
| `just fmt` | Format rust, python, and shell through the prek hooks |
| `just test-rust` | `cargo test --workspace` (DB tests run if `ROWX_TEST_DSN` is set) |
| `just test-py` | `uv run pytest` |
| `just db-up` / `just db-down` | Start or stop `postgres:18` (lands in T03) |
| `just bench` | Release build plus `bench/fetch_100k.py` (lands in T24) |

- Never run a plain `cargo build` of the cdylib, because it doesn't link on macOS. Use `uv run ...` or
  `uv run maturin develop`, and add `--release` for benchmarks.
- DB tests skip when `ROWX_TEST_DSN` is unset; with `ROWX_REQUIRE_DB=1`, as CI sets, they fail instead.

## Definition of done

1. `just check` and the relevant tests pass locally and CI is green.
2. New tests are named in the PR description. Behaviour without a test is not done.
3. Changes stay scoped to the feature. Small wiring edits (`mod` lines, `__init__` exports, `.pyi` additions) are
   fine and must be called out in the PR.
4. Errors use `thiserror` in `rowx-core` and `rowx-postgres`. They are mapped to Python exceptions only in an
   `errors.rs`: the shared exception-class registry is in `crates/rowx-py-core/src/errors.rs`, and each backend maps its
   own enum (`crates/rowx-postgres-py/src/errors.rs`).
5. Public Python is fully typed, and the `_core` stubs are updated with any new binding.

## Architecture conventions

- `rowx-core` and `rowx-postgres` have no pyo3 dependency. Wire codecs in `rowx-postgres/src/wire/` are pure functions
  over bytes with unit tests.
- The GIL is released around all IO (`Python::detach`), and IO runs on the shared tokio runtime in
  `rowx-py-core/src/runtime.rs`.
- Python has no runtime dependencies. The checker never imports or executes user code.
- DB tests are marked `@pytest.mark.db` and use the `schema` fixture, never `public`. Timing tests are marked
  `@pytest.mark.timing` and synchronise by polling `pg_stat_activity`, never with `sleep`.

## Cursor Cloud specific instructions

- Run Python through `uv run`. The image Python is older than `.python-version` (3.14); `uv` installs that
  interpreter and the project venv.
- Postgres 18 is already started on boot: plain container `rowx-pg` (state in `.rowx-test-dsn`) and TLS container
  `rowx-pg-ssl`. A login shell sources `$HOME/.rowx/env.sh` via `/etc/profile.d/rowx.sh`. Other shells need
  `. "$HOME/.rowx/env.sh"` before `just test-py` or `just test-rust`, which sets `ROWX_TEST_DSN`,
  `ROWX_TEST_DSN_SSL`, and `ROWX_TEST_CERTS`.
- `just db-up` is unnecessary while those containers are running (`scripts/pg.sh up` refuses when
  `.rowx-test-dsn` points at a live container). `just db-down` removes the plain container; the next boot start
  script creates it again.
- `just check` is the lint and typecheck gate (`prek`: ruff, ty, rumdl, cargo fmt, clippy).

## File ownership

| Area | Paths |
|---|---|
| Type-table metadata | `crates/rowx-postgres/src/types.rs` (append-only after T05) |
| Wire codecs (pure) | `crates/rowx-postgres/src/wire/<family>.rs` |
| PyObject codecs | `crates/rowx-postgres-py/src/codec/<family>.rs`; `codec/mod.rs` is dispatch only |
| Shared core (pure) | `crates/rowx-core/src/` (`state.rs` claim/settle state machine, `cancel.rs` plumbing, shared errors, pool) |
| Driver | `crates/rowx-postgres/src/{dsn,tls,conn,cancel,stmt_cache,exec}.rs` and its pool connector |
| Describe | `crates/rowx-postgres/src/{describe,explain}.rs`, `crates/rowx-postgres-py/src/describe.rs` |
| Bindings | `crates/rowx-postgres-py/src/{conn,exec,stream,pool,transaction,errors,types}.rs`, `crates/rowx-py-core/src/{runtime,errors}.rs`, `crates/rowx-py/src/lib.rs` |
| Python runtime (shared) | `src/rowx/{__init__,_query,_executor,errors,_template,_rowplan}.py` |
| Python Postgres backend | `src/rowx/postgres/` (including `_types.py` and `_vocab.py`) |
| Checker | `src/rowx/check/<module>.py`, one module each (including `migrate.py` and `database.py`, the CLI handlers, and `drift.py` and `scratch.py`, the migration checks) |
| Migrations (pure) | `crates/rowx-core/src/migrate/` (filename grammar, source resolution, checksums, plan) |
| Migrations (Postgres) | `crates/rowx-postgres/src/{migrate,admin}.rs`, `crates/rowx-postgres-py/src/{migrate,admin}.rs` |
| Python migrations | `Migrator`, `Migration` and `MigrationStatus` in `src/rowx/postgres/`; `MigrateError` and its subclasses in `src/rowx/errors.py` |
| Test plugin | `src/rowx/testing.py` (opt-in pytest plugin) |

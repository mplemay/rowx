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
  - *Files:* [configuration](prek.toml)
  - *Tools:* `rumdl` (markdown), `ruff` (python linting / formatting), `ty` (type checker), `cargo fmt` /
    `cargo clippy` (rust)
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
  - Keep [`_core.pyi`](src/rowx/_core.pyi) in sync with the rust `#[pymodule]`.
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

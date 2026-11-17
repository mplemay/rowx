# Contributing

rowx is three packages. `rowx-core` (`rowx_core`) has no runtime dependencies. `rowx-postgres` (`rowx_postgres`)
depends only on matching `rowx-core`. `rowx` is the CLI and the core re-exports, and `rowx[postgres]` installs
matching `rowx-postgres`. The tree is [Architecture](docs/contributing/architecture.md).

`uv sync` installs an editable workspace. `ty` checks each package where it lives. There is no merged type-check
tree and no cross-package symlink. Build the PostgreSQL extension with
`uv run maturin develop -m packages/rowx-postgres/Cargo.toml`. Never run a plain `cargo build` of the cdylib. See
[Distributions](docs/contributing/architecture.md#distributions).

Each package owns its tests. The directories, markers, and commands are [Testing](docs/contributing/testing.md).

Capabilities are optional and explicit. `Backend` has `checker`, `admin`, `migrations`, and `testing` factories. A
missing factory raises `UnsupportedCapabilityError`. A checker-only backend need not implement administration or
testing. There is no `ContextVar` and no implicit PostgreSQL fallback. See
[Acyclic dependencies](docs/contributing/architecture.md#acyclic-dependencies).

Each extension links `rowx-py-core` statically and owns its tokio runtime (`rowx-io`). Only the PostgreSQL extension
exists today. See [Native module and runtimes](docs/contributing/architecture.md#native-module-and-runtimes).

The [decision log](docs/contributing/decisions.md) records decisions, including ones later amended. Dependency
verdicts are [Dependency evaluation](docs/contributing/dependencies.md).

A change is done when:

- `just check` and the relevant tests pass.
- New tests are named in the pull request.
- Changes stay scoped to the feature.
- Public Python stays typed.

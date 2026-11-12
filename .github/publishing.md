# Publishing

`rowx`, `rowx-core`, and `rowx-postgres` publish from repository `mplemay/rowx` through workflow
`release.yml` and environment `pypi`.

Publish `rowx-core`, then `rowx-postgres`, then `rowx`.

`rowx` already uses the `pypi` environment. The owner registers pending trusted publishers for `rowx-core` and
`rowx-postgres`. This change does not publish.

Free-threaded CPython 3.14t cannot install the abi3 wheels, so pip builds rowx-postgres from the source distribution.

Issue #270 decides whether 0.3.0 also ships explicit cp314t wheels.

## rowx-embedded

`rowx-embedded` publishes from workflow `release-embedded.yml` and environment `pypi` on tags `embedded-vX.Y.Z`, where
`X.Y.Z` is `<pg major>.<pg minor>.<rev>` and matches `packages/rowx-embedded/pyproject.toml`, both `Cargo.toml` files
and `Cargo.lock`. It ships three wheels (macOS arm64, manylinux x86_64, manylinux aarch64) and no sdist. The owner
registers a pending trusted publisher for `rowx-embedded` with that workflow and environment first.

A manual run of the workflow is a dry run: it builds, checks (40 MB budget per wheel) and smoke tests every wheel and
never publishes. Publish `rowx-embedded` before a `rowx` release that ships the `embedded` extra.

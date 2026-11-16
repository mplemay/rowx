# Publishing

`rowx`, `rowx-core`, `rowx-postgres`, and `rowx-embedded` publish from repository `mplemay/rowx` through workflow
`release.yml` and one GitHub environment, `pypi`.

Each package is its own job. Publish order is `rowx-core`, then `rowx-postgres`, then `rowx-embedded`, then `rowx`. Each
job uses `skip-existing: true`. Re-running a failed publish job skips files already on PyPI and uploads the rest. If a
later publish job was skipped because an earlier one failed, re-run that skipped job after the failed one succeeds.

`rowx` already uses the `pypi` environment. The owner registers pending trusted publishers for
`rowx-core` and `rowx-postgres`.

`workflow_dispatch` builds, checks, and rebuilds the artifacts. It does not publish. Publishing runs
only on a `v*` tag push.

The abi3 wheels do not include a free-threaded cp314t build. Free-threaded CPython 3.14t cannot install
them, so pip builds `rowx-postgres` from the source distribution.

## rowx-embedded

`rowx-embedded` releases in lockstep with the other packages: the same `v*` tag, the same `release.yml` workflow and
the same `pypi` environment, with the same version as `packages/rowx-embedded/pyproject.toml`, both of its `Cargo.toml`
files and its `Cargo.lock`. It ships three wheels (macOS arm64, manylinux x86_64, manylinux aarch64) and no sdist. The
owner registers a pending trusted publisher for `rowx-embedded` with workflow `release.yml` and environment `pypi`
first.

The embedded wheels block the release: `rowx` publishes only after `rowx-embedded` does, and `rowx-embedded` publishes
after `rowx-postgres` because it pins it. A manual run of the workflow builds, checks (40 MB budget per wheel) and
smoke tests every wheel and never publishes. The old `embedded-v*` tags and `release-embedded.yml` are gone.

# Publishing

`rowx`, `rowx-core`, and `rowx-postgres` publish from repository `mplemay/rowx` through workflow
`release.yml` and one GitHub environment, `pypi`.

Publish order is `rowx-core`, then `rowx-postgres`, then `rowx`. `rowx` already uses the `pypi`
environment. The owner registers pending trusted publishers for `rowx-core` and `rowx-postgres`.

Each package is published from its own directory with `skip-existing: true`, so re-running a failed
publish skips files already on PyPI and uploads the rest.

`workflow_dispatch` builds, checks, and rebuilds the artifacts. It does not publish. Publishing runs
only on a `v*` tag push.

0.3.0 does not ship explicit cp314t wheels. Free-threaded CPython 3.14t cannot install the abi3
wheels, so pip builds `rowx-postgres` from the source distribution.

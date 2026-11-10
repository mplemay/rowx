# Publishing

`rowx`, `rowx-core`, and `rowx-postgres` publish from repository `mplemay/rowx` through workflow
`release.yml` and environment `pypi`.

Publish `rowx-core`, then `rowx-postgres`, then `rowx`.

`rowx` already uses the `pypi` environment. The owner registers pending trusted publishers for `rowx-core` and
`rowx-postgres`. This change does not publish.

Free-threaded CPython 3.14t cannot install the abi3 wheels, so pip builds rowx-postgres from the source distribution.

Issue #270 decides whether 0.3.0 also ships explicit cp314t wheels.

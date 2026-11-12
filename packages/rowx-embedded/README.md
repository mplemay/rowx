# rowx-embedded

An in-process PostgreSQL 18 engine host for rowx: no server, no Docker and no child process. It is for development,
checking and tests, never production.

The engine is PostgreSQL 18.4 compiled to WASIX and run by Wasmer inside the Python process. `rowx-embedded` owns one
dedicated engine thread per instance and a Unix-socket front proxy that serves one session at a time, so any
PostgreSQL client (including `rowx_postgres`) connects to `Instance.dsn`.

```python
from rowx_embedded import Instance

db = Instance.start()  # a memory database; Instance.start("/abs/path") persists
print(db.dsn)  # postgresql://postgres@/postgres?host=<private dir>&sslmode=disable
snapshot = db.snapshot()  # bytes; the instance keeps running
db.close()

copy = Instance.start(snapshot=snapshot)  # a fresh database with the same data
```

Limits: one session at a time (a second client gets `53300`), no query interruption, trust authentication only,
no `CREATE DATABASE` use, and `inet`/`cidr` binary values use WASI address-family bytes.

The package is built with `maturin` and published only as a per-platform wheel.

From this directory, build a wheel directly from the checkout with:

```sh
uv build --wheel
```

To build a source archive and then build a wheel from that archive, run:

```sh
uv build
```

Source archives include the vendored Wasmer compiler patch; Cargo downloads the remaining Rust dependencies from
the registry. Source builds require the Rust toolchain declared in `Cargo.toml`. Source archives are checked in CI
but are not published.

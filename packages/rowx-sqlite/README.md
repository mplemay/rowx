# rowx-sqlite

Typesafe raw SQL for Python 3.14+, with no ORM.

SQLite backend for rowx. This release only wires the package into the workspace; every capability raises
`UnsupportedCapabilityError` until the backend lands.

`rowx-sqlite` depends only on matching `rowx-core`.

```sh
uv add rowx-core rowx-sqlite
```

`rowx[sqlite]` is the checker plus this backend.

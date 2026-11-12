# rowx-postgres

You keep the SQL, name the row in ordinary Python, and the build fails when Postgres disagrees. This package is the
async Postgres driver that runs the query.

`rowx-postgres` depends only on matching `rowx-core`.

```python
from rowx_postgres import Pool, connect
```

A standalone application installs `rowx-core` and `rowx-postgres`. That standalone pair does not include the `rowx`
command.

```sh
uv add rowx-core rowx-postgres
```

`rowx[postgres]` is the checker plus this backend.

Free-threaded CPython 3.14t cannot install the abi3 wheels, so pip builds rowx-postgres from the source distribution.

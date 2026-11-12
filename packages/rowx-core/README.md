# rowx-core

You keep the SQL, name the row in ordinary Python, and the build fails when Postgres disagrees. This package is the
shared query API. It does not include a driver or the `rowx` command.

`rowx-core` is pure Python and has no runtime dependencies. `from rowx import query` and
`from rowx_core import query` are the same object. The checker is the `rowx` distribution.

```python
from rowx_core import query
```

Install the checker and the PostgreSQL backend with `rowx[postgres]`.

A standalone application installs `rowx-core` and `rowx-postgres`. That standalone pair does not include the `rowx`
command.

```sh
uv add rowx-core rowx-postgres
```

# rowx-core

Typesafe raw SQL for Python 3.14+, with no ORM.

Shared typed query, executor, row-shape and error primitives for rowx database implementations.

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

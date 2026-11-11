# Pool reset

See [Security](../references/security.md), [Pool reset](../../../../docs/guide/security.md#pool-reset), and
[Hooks and the reset](../../../../docs/guide/executors.md#hooks-and-the-reset).

## Contents

- Leave the default reset in place
- A custom query replaces the default

---

## `reset_query=None` is unsafe for a shared pool

When a connection returns to the pool, the next checkout inherits session state. The default `RESET_QUERY` clears
session authorization, settings, advisory locks, cursors, `LISTEN`s, and temporary tables. `reset_query=None` is
unsafe for a pool that serves more than one tenant or role: nothing then clears `SET ROLE`, `search_path`, temporary
tables, advisory locks, or custom settings.

Incorrect:

```python
from rowx_postgres import Pool


def shared_pool(dsn: str) -> Pool:
    return Pool(dsn, max_size=10, reset_query=None)
```

Correct:

```python
from rowx_postgres import Pool


def shared_pool(dsn: str) -> Pool:
    return Pool(dsn, max_size=10)
```

## A custom `reset_query` replaces the default

Any string passed as `reset_query` replaces `RESET_QUERY` entirely. `RESET ALL` alone does not reset session
authorization, and it does not discard temporary tables, advisory locks, cursors, or `LISTEN`s. Keep every part you
rely on. The statement list is [Hooks and the reset](../../../../docs/guide/executors.md#hooks-and-the-reset).

Incorrect:

```python
from rowx_postgres import Pool


def shared_pool(dsn: str) -> Pool:
    return Pool(dsn, reset_query="RESET ALL")
```

Correct:

```python
from rowx_postgres import RESET_QUERY, Pool


def shared_pool(dsn: str) -> Pool:
    return Pool(dsn, reset_query=RESET_QUERY)
```

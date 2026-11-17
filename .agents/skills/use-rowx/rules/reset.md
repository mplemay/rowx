# Pool reset

See [Security](../references/security.md).

## Contents

- Leave the default reset in place
- A custom query replaces the default

---

## `reset_query=None` is unsafe for a shared pool

When a connection returns to the pool, the next checkout inherits session state. The default `RESET_QUERY` clears
the session user and the role (cleared by `RESET SESSION AUTHORIZATION`, not by `RESET ALL`), settings, advisory
locks, cursors, `LISTEN`s, and temporary tables. `reset_query=None` is unsafe for a pool that serves more than one
tenant or role: nothing then clears `SET ROLE`, `search_path`, temporary tables, advisory locks, or custom settings.

Incorrect:

```python
from rowx.postgres import Pool


def shared_pool(dsn: str) -> Pool:
    return Pool(dsn, max_size=10, reset_query=None)
```

Correct:

```python
from rowx.postgres import Pool


def shared_pool(dsn: str) -> Pool:
    return Pool(dsn, max_size=10)
```

## A custom `reset_query` replaces the default

Any string passed as `reset_query` replaces `RESET_QUERY` entirely.

`RESET ALL` resets neither the role nor session authorization.

It still does not discard temporary tables, advisory locks, cursors, or `LISTEN`s. Keep every part you rely on. The
default is `RESET_QUERY`:

```text
RESET SESSION AUTHORIZATION; RESET ALL; SELECT pg_catalog.pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; DISCARD TEMP;
```

A custom `reset_query` replaces that list.

Incorrect:

```python
from rowx.postgres import Pool


def shared_pool(dsn: str) -> Pool:
    return Pool(dsn, reset_query="RESET ALL")
```

Correct:

```python
from rowx.postgres import RESET_QUERY, Pool


def shared_pool(dsn: str) -> Pool:
    return Pool(dsn, reset_query=RESET_QUERY)
```

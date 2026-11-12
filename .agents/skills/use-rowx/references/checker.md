# Checker

## Exit codes

| Code | Meaning |
|---|---|
| 0 | No diagnostics, including when no queries were found. Also warnings alone when `--exit-zero-on-warning` is set or `error-on-warning` is false. |
| 1 | One or more diagnostics. Warnings fail the check by default. |
| 2 | Bad invocation or config, an unreachable database, a describe failure that is not an SQL error, a drift or scratch failure, or an internal error. |

A missing database URL is exit 2 only when at least one statement would be described.

## What it checks

`rowx check` uses `--database-url` or `DATABASE_URL` (`database_url_env` defaults to `DATABASE_URL`). It parses
files and does not import or execute application code. It prepares and describes `query` from `rowx` and
`rowx_core`. `query_unchecked` SQL is not described. `T` is still resolved.

There is no implicit Postgres fallback and no ContextVar for the target. The executor that runs a query is the one
the method receives. The checker does not infer that executor.

```python
from rowx import query


def names():
    return query[str](t"SELECT name FROM users ORDER BY name")
```

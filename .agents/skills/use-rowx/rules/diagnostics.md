# Diagnostics

See [The checker](../references/checker.md), [the checker guide](../../../../docs/guide/checker.md), and
[the checker reference](../../../../docs/reference/checker.md).

## Contents

- Exit codes
- What is described
- The checker does not import the application

---

## Exit codes

`rowx check` exits 0 when it reports no diagnostics, including when no queries were found. Warnings alone also exit
0 when `--exit-zero-on-warning` is set or `[tool.rowx.terminal] error-on-warning` is false. It exits 1 when it
reports one or more diagnostics. Warnings fail the check by default. It exits 2 on a bad invocation or config, an
unreachable database, a describe failure that is not an SQL error, a drift or scratch failure, or an internal error.

A missing database URL is exit 2 only when at least one statement would be described. A file whose calls are all
`query_unchecked` does not need one.

Incorrect:

```python
def interpret(code: int, query_count: int) -> str:
    if query_count == 0 or code == 2:
        return "ok"
    return "diagnostics"
```

Correct:

```python
def interpret(code: int) -> str:
    if code == 0:
        return "no diagnostics"
    if code == 1:
        return "diagnostics"
    return "invocation, database, drift, scratch, or internal failure"
```

## `query_unchecked` SQL is not described

The checker parses source. It does not import or execute application code. It prepares and describes `query` on both
`from rowx import query` and `from rowx_core import query`. `query_unchecked` SQL is not described. The checker still
resolves `T`. There is no implicit Postgres target: the database is `--database-url` or the variable named by
`database_url_env`, which defaults to `DATABASE_URL`.

Incorrect:

```python
from rowx import query


def names(sql: str):
    return query[str](sql)
```

Correct:

```python
from rowx import query


def names():
    return query[str](t"SELECT name FROM users ORDER BY name")
```

The checker finds the t-string by parsing the file. It does not import the application to discover the SQL. A
`query_unchecked` call is not described; keep it in a small function that allowlists its dynamic parts. See
[Identifiers](identifiers.md).

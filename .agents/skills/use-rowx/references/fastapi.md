# FastAPI

Hold one `Pool` for the process. Create it in the lifespan, after a forking server has forked. A pool used in a
forked child raises `InterfaceError`. `Pool` comes from `rowx.postgres`. `Pool` on `rowx` is the executor protocol
([Imports](../rules/imports.md)).

The lifespan's `async with Pool(...)` calls `open()` on the way in and `await pool.close()` on the way out. That
close is the shutdown. `terminate()` drops connections without waiting for them. The lifespan uses `close`.

`DATABASE_URL` is the same variable `rowx check` reads. There is no implicit Postgres target.

A query on a pool checks out a connection for that one call. Two calls are two checkouts, and they are not one
transaction. Depend on the pool for a single statement. Depend on `pool.acquire()` when several statements share a
connection. Depend on `pool.transaction()` when they commit or roll back together. `conn.transaction()` is the same
transaction helper on a connection you already hold.

`rowx check` parses the route module and does not import FastAPI. The query is checked like any other
`query[T](t"...")`.

Behind a transaction-mode pooler, build the pool with
`Pool(ConnectOptions.from_dsn(url, statement_cache_size=0), min_size=1)`. A session `SET` does not survive the next
checkout there. Read [Pool reset](../rules/reset.md) before passing `reset_query=None`.

The block below is the rowx half: lifespan, a pool query, a shared connection, a transaction, and a closed pool.
The FastAPI app under it is the same calls with `Depends`.

```python test
import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from rowx import query
from rowx.postgres import Pool


@dataclass(frozen=True, slots=True)
class User:
    id: int
    name: str
    email: str | None = None


@asynccontextmanager
async def lifespan() -> AsyncGenerator[Pool]:
    async with Pool(DSN, min_size=1) as pool:
        yield pool


async def read_user(pool: Pool, name: str) -> User | None:
    return await query[User](
        t"SELECT id, name, email FROM users WHERE name = {name}",
    ).optional(pool)


async def main() -> None:
    async with lifespan() as pool:
        alice = await read_user(pool, "alice")
        assert alice is not None
        assert alice.name == "alice"
        assert alice.email == "alice@example.com"
        assert await read_user(pool, "missing") is None

        async with pool.acquire() as conn:
            carol = await query[str](t"SELECT name FROM users WHERE name = {'carol'}").one(conn)
            assert carol == "carol"

        async with pool.transaction() as tx:
            updated = await query(
                t"UPDATE users SET email = {'carol@example.com'} WHERE name = {'carol'}",
            ).execute(tx)
            assert updated.rows_affected == 1
            await tx.rollback()

        carol_row = await read_user(pool, "carol")
        assert carol_row is not None
        assert carol_row.email is None
    assert pool.closed


asyncio.run(main())
```

## The app

`.optional` returns `T | None`. `.one` raises `RowNotFound` from `rowx` or `rowx.errors`. FastAPI does not map that
exception to a status code. Raise `HTTPException` in the path operation.

A dependency that yields `pool.transaction()` commits when the path operation returns and rolls back when it
raises, including `HTTPException`. `scope="function"` finishes that exit before the response is sent, so a failed
commit is still the response. The default scope for a yield dependency runs the exit after the response has gone
out.

The row can be the dataclass below, or a pydantic model ([From SQLAlchemy](sqlalchemy.md)). FastAPI serializes
either as the response.

```python
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from rowx import query
from rowx.postgres import Connection, Pool, Transaction


@dataclass(frozen=True, slots=True)
class User:
    id: int
    name: str
    email: str | None = None


@dataclass(frozen=True, slots=True)
class EmailUpdate:
    email: str


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    async with Pool(os.environ["DATABASE_URL"], min_size=1) as pool:
        app.state.pool = pool
        yield


app = FastAPI(lifespan=lifespan)


def get_pool(request: Request) -> Pool:
    pool = request.app.state.pool
    assert isinstance(pool, Pool)
    return pool


async def get_connection(pool: Annotated[Pool, Depends(get_pool)]) -> AsyncGenerator[Connection]:
    async with pool.acquire() as conn:
        yield conn


async def get_transaction(pool: Annotated[Pool, Depends(get_pool)]) -> AsyncGenerator[Transaction]:
    async with pool.transaction() as tx:
        yield tx


@app.get("/users/{name}")
async def read_user(name: str, pool: Annotated[Pool, Depends(get_pool)]) -> User:
    user = await query[User](
        t"SELECT id, name, email FROM users WHERE name = {name}",
    ).optional(pool)
    if user is None:
        raise HTTPException(status_code=404)
    return user


@app.get("/users/{name}/posts")
async def read_posts(name: str, conn: Annotated[Connection, Depends(get_connection)]) -> list[str]:
    owner = await query[int](t"SELECT id FROM users WHERE name = {name}").optional(conn)
    if owner is None:
        raise HTTPException(status_code=404)
    return await query[str](
        t"SELECT title FROM posts WHERE user_id = {owner} ORDER BY title",
    ).all(conn)


@app.post("/users/{name}/email")
async def replace_email(
    name: str,
    body: EmailUpdate,
    tx: Annotated[Transaction, Depends(get_transaction, scope="function")],
) -> User:
    email = body.email
    updated = await query(
        t"UPDATE users SET email = {email} WHERE name = {name}",
    ).execute(tx)
    if updated.rows_affected != 1:
        raise HTTPException(status_code=404)
    return await query[User](
        t"SELECT id, name, email FROM users WHERE name = {name}",
    ).one(tx)
```

`EmailUpdate` is the body. Bind `body.email` through a local `{email}`. A pydantic model works as that body
too. The value rowx sends is still a parameter.

Each worker process runs the lifespan and owns the pool it opened. `max_size` is that process's limit.

## Tests

A test calls the same `query` the path operation calls, and passes a `Pool` or a connection in. `rowx.testing`
gives each test a migrated database on the synchronous fixtures `rowx_pool` and `rowx_connection`. An async test
uses `asyncio.run`, or pytest-asyncio on asyncio. Install both extras when the test needs the plugin and a
database: `uv add "rowx[postgres,pytest]"`. Set `pytest_plugins = ["rowx.testing"]`. The fixture list and the
skip rules are [Testing](testing.md).

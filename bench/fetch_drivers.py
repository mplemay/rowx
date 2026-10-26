import asyncio
import os
import statistics
import sys
from datetime import datetime
from time import perf_counter
from typing import TYPE_CHECKING, Final
from urllib.parse import ParseResult, urlparse, urlunparse

import asyncpg
import psycopg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import rowx
from rowx.postgres import connect

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

ROWS: Final[int] = 100_000
RUNS: Final[int] = 7
# 0 prepares on the first execution, so the untimed warmup fills the cache
PREPARE_THRESHOLD: Final[int] = 0
POSTGRES_SCHEMES: Final[frozenset[str]] = frozenset({"postgres", "postgresql"})
DROP_SQL: Final[str] = "DROP TABLE IF EXISTS rowx_bench_100k"
CREATE_SQL: Final[str] = (
    "CREATE TABLE rowx_bench_100k (id int4 NOT NULL, body text NOT NULL, created_at timestamptz NOT NULL)"
)
INSERT_SQL: Final[str] = """
INSERT INTO rowx_bench_100k
SELECT i, 'row ' || i, now() + make_interval(secs => i) FROM generate_series(1, 100000) i
"""
SELECT_SQL: Final[str] = "SELECT id, body, created_at FROM rowx_bench_100k"
DRIVER_WIDTH: Final[int] = 22
MS_WIDTH: Final[int] = 8
RATIO_WIDTH: Final[int] = 12


async def median_ms(run: Callable[[], Awaitable[int]]) -> float:
    # one untimed warmup so the statement is prepared and cached
    await run()
    samples: list[float] = []
    for _ in range(RUNS):
        start = perf_counter()
        count = await run()
        samples.append((perf_counter() - start) * 1000)
        if count != ROWS:
            msg = f"expected {ROWS} rows, got {count}"
            raise RuntimeError(msg)
    return statistics.median(samples)


def sqlalchemy_url(dsn: str, driver: str) -> str:
    parsed = urlparse(dsn)
    if parsed.scheme not in POSTGRES_SCHEMES:
        msg = f"unsupported DSN scheme {parsed.scheme!r}"
        raise ValueError(msg)
    return urlunparse(
        ParseResult(
            f"postgresql+{driver}",
            parsed.netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        ),
    )


async def time_asyncpg(dsn: str) -> float:
    conn = await asyncpg.connect(dsn)
    try:

        async def run() -> int:
            return len(await conn.fetch(SELECT_SQL))

        return await median_ms(run)
    finally:
        await conn.close()


async def time_psycopg(dsn: str) -> float:
    # autocommit avoids BEGIN/COMMIT around a read
    conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True, prepare_threshold=PREPARE_THRESHOLD)
    try:

        async def run() -> int:
            async with conn.cursor() as cur:
                await cur.execute(SELECT_SQL)
                return len(await cur.fetchall())

        return await median_ms(run)
    finally:
        await conn.close()


async def time_sqlalchemy(url: str, *, prepare_threshold: int | None = None) -> float:
    # AUTOCOMMIT keeps the read out of a transaction. prepare_threshold is psycopg-only.
    connect_args: dict[str, int] = {}
    if prepare_threshold is not None:
        connect_args["prepare_threshold"] = prepare_threshold
    engine = create_async_engine(
        url,
        pool_size=1,
        max_overflow=0,
        isolation_level="AUTOCOMMIT",
        connect_args=connect_args,
    )
    statement = text(SELECT_SQL)
    try:
        async with engine.connect() as conn:

            async def run() -> int:
                result = await conn.execute(statement)
                return len(result.all())

            return await median_ms(run)
    finally:
        await engine.dispose()


async def time_rowx(dsn: str) -> float:
    select = rowx.query[tuple[int, str, datetime]](t"SELECT id, body, created_at FROM rowx_bench_100k")
    async with await connect(dsn) as conn:

        async def run() -> int:
            return len(await select.all(conn))

        return await median_ms(run)


def report(results: list[tuple[str, float]], baseline_ms: float) -> None:
    lines = [f"{'driver':<{DRIVER_WIDTH}}{'ms':>{MS_WIDTH}}{'vs_asyncpg':>{RATIO_WIDTH}}"]
    lines.extend(f"{name:<{DRIVER_WIDTH}}{ms:{MS_WIDTH}.1f}{ms / baseline_ms:{RATIO_WIDTH}.2f}" for name, ms in results)
    print("\n".join(lines))  # ruff: ignore[print]


async def main(dsn: str) -> None:
    setup = await asyncpg.connect(dsn)
    await setup.execute(DROP_SQL)
    await setup.execute(CREATE_SQL)
    await setup.execute(INSERT_SQL)
    try:
        asyncpg_ms = await time_asyncpg(dsn)
        results = [
            ("asyncpg", asyncpg_ms),
            ("psycopg", await time_psycopg(dsn)),
            ("sqlalchemy+asyncpg", await time_sqlalchemy(sqlalchemy_url(dsn, "asyncpg"))),
            (
                "sqlalchemy+psycopg",
                await time_sqlalchemy(sqlalchemy_url(dsn, "psycopg"), prepare_threshold=PREPARE_THRESHOLD),
            ),
            ("rowx", await time_rowx(dsn)),
        ]
        report(results, asyncpg_ms)
    finally:
        await setup.execute(DROP_SQL)
        await setup.close()


if __name__ == "__main__":
    if not (dsn := os.environ.get("ROWX_TEST_DSN")):
        print("ROWX_TEST_DSN is not set", file=sys.stderr)  # ruff: ignore[print]
        sys.exit(2)
    asyncio.run(main(dsn))

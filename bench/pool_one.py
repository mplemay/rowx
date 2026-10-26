import asyncio
import os
import statistics
import sys
from time import perf_counter
from typing import TYPE_CHECKING, Final

import asyncpg

import rowx
from rowx.postgres import Pool, connect

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

TASKS: Final[int] = 64
PER_TASK: Final[int] = 200
RUNS: Final[int] = 7
LATENCY_CALLS: Final[int] = 2000
POOL_SIZE: Final[int] = 16
SELECT_SQL: Final[str] = "SELECT $1::int4"


async def median_throughput(run: Callable[[], Awaitable[None]]) -> float:
    # one untimed warmup so statements are prepared and cached
    await run()
    samples: list[float] = []
    for _ in range(RUNS):
        start = perf_counter()
        await run()
        samples.append(TASKS * PER_TASK / (perf_counter() - start))
    return statistics.median(samples)


async def latency_us(call: Callable[[], Awaitable[object]]) -> float:
    await call()
    start = perf_counter()
    for _ in range(LATENCY_CALLS):
        await call()
    return (perf_counter() - start) / LATENCY_CALLS * 1e6


async def concurrent(call: Callable[[], Awaitable[object]]) -> None:
    async def worker() -> None:
        for _ in range(PER_TASK):
            await call()

    await asyncio.gather(*(worker() for _ in range(TASKS)))


async def main(dsn: str) -> None:
    select = rowx.query[tuple[int]](t"SELECT {1}::int4")

    async with (
        Pool(dsn, min_size=POOL_SIZE, max_size=POOL_SIZE) as pool,
        Pool(dsn, min_size=POOL_SIZE, max_size=POOL_SIZE, reset_query=None) as bare_pool,
        asyncpg.create_pool(dsn, min_size=POOL_SIZE, max_size=POOL_SIZE) as apool,
        await connect(dsn) as conn,
    ):

        async def rowx_call() -> object:
            return await select.one(pool)

        async def bare_call() -> object:
            return await select.one(bare_pool)

        async def asyncpg_call() -> object:
            return await apool.fetchrow(SELECT_SQL, 1)

        async def conn_call() -> object:
            return await select.one(conn)

        rowx_tps = await median_throughput(lambda: concurrent(rowx_call))
        bare_tps = await median_throughput(lambda: concurrent(bare_call))
        asyncpg_tps = await median_throughput(lambda: concurrent(asyncpg_call))
        conn_us = await latency_us(conn_call)
        pool_us = await latency_us(rowx_call)

        # time to acquire an idle connection, which is the wait a caller sees when the pool is warm
        waits: list[float] = []
        for _ in range(LATENCY_CALLS):
            start = perf_counter()
            async with pool.acquire():
                waits.append((perf_counter() - start) * 1e6)
        acquire_us = statistics.median(waits)

    print("rowx_qps asyncpg_qps ratio no_reset_qps reset_cost_pct conn_us pool_us acquire_us")  # ruff: ignore[print]
    print(  # ruff: ignore[print]
        f"{rowx_tps:.0f} {asyncpg_tps:.0f} {rowx_tps / asyncpg_tps:.2f} {bare_tps:.0f} "
        f"{(1 - rowx_tps / bare_tps) * 100:.1f} {conn_us:.0f} {pool_us:.0f} {acquire_us:.0f}",
    )


if __name__ == "__main__":
    if not (dsn := os.environ.get("ROWX_TEST_DSN")):
        print("ROWX_TEST_DSN is not set", file=sys.stderr)  # ruff: ignore[print]
        sys.exit(2)
    asyncio.run(main(dsn))

import asyncio
import os
import statistics
import sys
from datetime import datetime
from time import perf_counter
from typing import TYPE_CHECKING, Final

import asyncpg

import rowx

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

ROWS: Final[int] = 100_000
RUNS: Final[int] = 7
DROP_SQL: Final[str] = "DROP TABLE IF EXISTS rowx_bench_100k"
CREATE_SQL: Final[str] = (
    "CREATE TABLE rowx_bench_100k (id int4 NOT NULL, body text NOT NULL, created_at timestamptz NOT NULL)"
)
INSERT_SQL: Final[str] = """
INSERT INTO rowx_bench_100k
SELECT i, 'row ' || i, now() + make_interval(secs => i) FROM generate_series(1, 100000) i
"""
SELECT_SQL: Final[str] = "SELECT id, body, created_at FROM rowx_bench_100k"


async def median_ms(run: Callable[[], Awaitable[int]]) -> float:
    # one untimed warmup so the statement is prepared and cached for both drivers
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


async def main(dsn: str) -> None:
    setup = await asyncpg.connect(dsn)
    await setup.execute(DROP_SQL)
    await setup.execute(CREATE_SQL)
    await setup.execute(INSERT_SQL)
    try:
        select = rowx.query[tuple[int, str, datetime]](t"SELECT id, body, created_at FROM rowx_bench_100k")

        async with await rowx.connect(dsn) as conn:

            async def run_rowx() -> int:
                return len(await select.fetch_all(conn))

            rowx_ms = await median_ms(run_rowx)

        async with asyncpg.create_pool(dsn, min_size=1, max_size=1) as pool:

            async def run_asyncpg() -> int:
                return len(await pool.fetch(SELECT_SQL))

            asyncpg_ms = await median_ms(run_asyncpg)
    finally:
        await setup.execute(DROP_SQL)
        await setup.close()

    print("rowx_ms asyncpg_ms ratio")  # noqa: T201
    print(f"{rowx_ms:.1f} {asyncpg_ms:.1f} {rowx_ms / asyncpg_ms:.2f}")  # noqa: T201


if __name__ == "__main__":
    if not (dsn := os.environ.get("ROWX_TEST_DSN")):
        print("ROWX_TEST_DSN is not set", file=sys.stderr)  # noqa: T201
        sys.exit(2)
    asyncio.run(main(dsn))

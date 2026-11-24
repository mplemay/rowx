import asyncio
import sqlite3
import statistics
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import TYPE_CHECKING, Any, Final

from rowx_sqlite import ConnectOptions, connect

import rowx

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from rowx_sqlite import Connection

ROWS: Final[int] = 100_000
RUNS: Final[int] = 7
CREATE_SQL: Final[str] = "CREATE TABLE rowx_bench_100k (id INTEGER NOT NULL, body TEXT NOT NULL, score REAL NOT NULL)"
INSERT_SQL: Final[str] = """
WITH RECURSIVE series (i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM series WHERE i < 100000)
INSERT INTO rowx_bench_100k SELECT i, 'row ' || i, i * 0.5 FROM series
"""


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


def count_all(statement: rowx.query[Any], conn: Connection) -> Callable[[], Awaitable[int]]:
    async def run() -> int:
        return len(await statement.all(conn))

    return run


def count_fetch(statement: rowx.query[Any], conn: Connection) -> Callable[[], Awaitable[int]]:
    async def run() -> int:
        return len([row async for row in statement.fetch(conn)])

    return run


async def main(database: Path) -> None:
    with closing(sqlite3.connect(database)) as setup:
        setup.execute(CREATE_SQL)
        setup.execute(INSERT_SQL)
        setup.commit()

    select = rowx.query[tuple[int, str, float]](t"SELECT id, body, score FROM rowx_bench_100k")
    scalar = rowx.query[int](t"SELECT id FROM rowx_bench_100k")

    async with await connect(ConnectOptions(path=str(database))) as conn:
        all_ms = await median_ms(count_all(select, conn))
        fetch_ms = await median_ms(count_fetch(select, conn))
        scalar_ms = await median_ms(count_all(scalar, conn))
        scalar_fetch_ms = await median_ms(count_fetch(scalar, conn))

    print("all_ms fetch_ms fetch_vs_all scalar_ms scalar_fetch_ms")  # ruff: ignore[print]
    print(f"{all_ms:.1f} {fetch_ms:.1f} {fetch_ms / all_ms:.2f} {scalar_ms:.1f} {scalar_fetch_ms:.1f}")  # ruff: ignore[print]


if __name__ == "__main__":
    if len(sys.argv) != 1:
        print("usage: fetch_100k_sqlite.py", file=sys.stderr)  # ruff: ignore[print]
        sys.exit(2)
    with TemporaryDirectory(prefix="rowx_bench_") as directory:
        asyncio.run(main(Path(directory) / "bench.db"))

import asyncio
import os
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from typing import TYPE_CHECKING, Any, Final, NamedTuple, TypedDict

import asyncpg
from pydantic import BaseModel
from pydantic.dataclasses import dataclass as pydantic_dataclass

import rowx
from rowx.postgres import connect

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from rowx.postgres import Connection

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


@dataclass(slots=True)
class DataclassRow:
    id: int
    body: str
    created_at: datetime


class NamedTupleRow(NamedTuple):
    id: int
    body: str
    created_at: datetime


class TypedDictRow(TypedDict):
    id: int
    body: str
    created_at: datetime


class ModelRow(BaseModel):
    id: int
    body: str
    created_at: datetime


@pydantic_dataclass
class PydanticDataclassRow:
    id: int
    body: str
    created_at: datetime


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


def count_all(statement: rowx.query[Any], conn: Connection) -> Callable[[], Awaitable[int]]:
    async def run() -> int:
        return len(await statement.all(conn))

    return run


async def main(dsn: str) -> None:
    setup = await asyncpg.connect(dsn)
    await setup.execute(DROP_SQL)
    await setup.execute(CREATE_SQL)
    await setup.execute(INSERT_SQL)
    try:
        select = rowx.query[tuple[int, str, datetime]](t"SELECT id, body, created_at FROM rowx_bench_100k")

        scalar = rowx.query[int](t"SELECT id FROM rowx_bench_100k")

        named = {
            "dataclass": rowx.query[DataclassRow](t"SELECT id, body, created_at FROM rowx_bench_100k"),
            "namedtuple": rowx.query[NamedTupleRow](t"SELECT id, body, created_at FROM rowx_bench_100k"),
            "typeddict": rowx.query[TypedDictRow](t"SELECT id, body, created_at FROM rowx_bench_100k"),
            "model": rowx.query[ModelRow](t"SELECT id, body, created_at FROM rowx_bench_100k"),
            "pydantic_dataclass": rowx.query[PydanticDataclassRow](t"SELECT id, body, created_at FROM rowx_bench_100k"),
        }
        named_ms: dict[str, float] = {}

        async with await connect(dsn) as conn:

            async def run_rowx() -> int:
                return len(await select.all(conn))

            rowx_ms = await median_ms(run_rowx)

            async def run_rowx_fetch() -> int:
                return len([row async for row in select.fetch(conn)])

            fetch_ms = await median_ms(run_rowx_fetch)

            async def run_scalar() -> int:
                return len(await scalar.all(conn))

            scalar_ms = await median_ms(run_scalar)

            async def run_scalar_fetch() -> int:
                return len([row async for row in scalar.fetch(conn)])

            scalar_fetch_ms = await median_ms(run_scalar_fetch)

            for name, statement in named.items():
                named_ms[name] = await median_ms(count_all(statement, conn))

        async with asyncpg.create_pool(dsn, min_size=1, max_size=1) as pool:

            async def run_asyncpg() -> int:
                return len(await pool.fetch(SELECT_SQL))

            asyncpg_ms = await median_ms(run_asyncpg)
    finally:
        await setup.execute(DROP_SQL)
        await setup.close()

    print(  # ruff: ignore[print]
        "rowx_ms asyncpg_ms ratio fetch_ms fetch_vs_all scalar_ms scalar_fetch_ms "
        + " ".join(f"{n}_ms" for n in named_ms),
    )
    print(  # ruff: ignore[print]
        f"{rowx_ms:.1f} {asyncpg_ms:.1f} {rowx_ms / asyncpg_ms:.2f} {fetch_ms:.1f} {fetch_ms / rowx_ms:.2f}"
        f" {scalar_ms:.1f} {scalar_fetch_ms:.1f} " + " ".join(f"{ms:.1f}" for ms in named_ms.values()),
    )


if __name__ == "__main__":
    if not (dsn := os.environ.get("ROWX_TEST_DSN")):
        print("ROWX_TEST_DSN is not set", file=sys.stderr)  # ruff: ignore[print]
        sys.exit(2)
    asyncio.run(main(dsn))

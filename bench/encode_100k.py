import asyncio
import os
import statistics
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from ipaddress import IPv4Address
from time import perf_counter
from typing import TYPE_CHECKING, Any, Final

import rowx
from rowx.postgres import connect

if TYPE_CHECKING:
    from collections.abc import Callable

ELEMENTS: Final[int] = 100_000
RUNS: Final[int] = 7
BASE: Final[datetime] = datetime(2024, 1, 1, tzinfo=UTC)

# one 100k-element array parameter per type; the server only reports the cardinality, so the time is the encode
CASES: Final[dict[str, list[Any]]] = {
    "numeric[]": [Decimal(f"{i}.{i % 1000:03d}") for i in range(ELEMENTS)],
    "text[]": [f'row {i} a"b,c' for i in range(ELEMENTS)],
    "jsonb[]": [{"id": i, "name": f"n{i}"} for i in range(ELEMENTS)],
    "timestamptz[]": [BASE + timedelta(seconds=i) for i in range(ELEMENTS)],
    "interval[]": [timedelta(days=i % 30, seconds=i, microseconds=i % 1000) for i in range(ELEMENTS)],
    "inet[]": [IPv4Address(i) for i in range(ELEMENTS)],
    "bytea[]": [i.to_bytes(4) * 4 for i in range(ELEMENTS)],
}

QUERIES: Final[dict[str, Callable[[list[Any]], rowx.query[int]]]] = {
    "numeric[]": lambda values: rowx.query[int](t"SELECT cardinality({values}::numeric[])"),
    "text[]": lambda values: rowx.query[int](t"SELECT cardinality({values}::text[])"),
    "jsonb[]": lambda values: rowx.query[int](t"SELECT cardinality({values}::jsonb[])"),
    "timestamptz[]": lambda values: rowx.query[int](t"SELECT cardinality({values}::timestamptz[])"),
    "interval[]": lambda values: rowx.query[int](t"SELECT cardinality({values}::interval[])"),
    "inet[]": lambda values: rowx.query[int](t"SELECT cardinality({values}::inet[])"),
    "bytea[]": lambda values: rowx.query[int](t"SELECT cardinality({values}::bytea[])"),
}


async def main(dsn: str) -> None:
    async with await connect(dsn) as conn:
        print("type median_ms min_ms")  # ruff: ignore[print]
        for sql_type, values in CASES.items():
            count = QUERIES[sql_type](values)
            samples: list[float] = []
            # the first run is untimed so the statement is prepared and cached
            for run in range(RUNS + 1):
                start = perf_counter()
                result = await count.one(conn)
                elapsed = (perf_counter() - start) * 1000
                if result != ELEMENTS:
                    msg = f"expected {ELEMENTS} elements, got {result}"
                    raise RuntimeError(msg)
                if run:
                    samples.append(elapsed)
            print(f"{sql_type} {statistics.median(samples):.1f} {min(samples):.1f}")  # ruff: ignore[print]


if __name__ == "__main__":
    if not (dsn := os.environ.get("ROWX_TEST_DSN")):
        print("ROWX_TEST_DSN is not set", file=sys.stderr)  # ruff: ignore[print]
        sys.exit(2)
    asyncio.run(main(dsn))

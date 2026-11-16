#!/usr/bin/env python3
from asyncio import run
from importlib.util import find_spec
from pathlib import Path
from sys import prefix
from typing import Final

from rowx_core import query
from rowx_embedded import Instance
from rowx_postgres import connect

# the engine starts, a plain socket DSN reaches it through the rowx driver, and no rowx CLI package is installed
EXPECTED: Final[int] = 1


async def select_one(dsn: str) -> int:
    async with await connect(dsn) as conn:
        return await query[int](t"SELECT 1").one(conn)


def main() -> int:
    if find_spec("rowx") is not None:
        message = "the rowx CLI package must not be installed for the companion smoke test"
        raise SystemExit(message)
    engine = Path(find_spec("rowx_embedded._engine").origin or "").resolve()  # ty: ignore[unresolved-attribute]
    if not engine.is_relative_to(Path(prefix).resolve()):
        message = f"rowx_embedded was imported from outside the clean venv: {engine}"
        raise SystemExit(message)
    with Instance.start() as db:
        found = run(select_one(db.dsn))
    if found != EXPECTED:
        message = f"SELECT 1 returned {found!r}"
        raise SystemExit(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

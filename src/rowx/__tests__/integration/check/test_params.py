import asyncio
from typing import Final

import pytest

from rowx.__tests__.paramgen import build, direct, infer, run, runtime_shown, shown
from rowx.__tests__.sourcegen import seeded
from rowx.postgres import connect

pytestmark = [pytest.mark.integration, pytest.mark.db]

SEEDS: Final = range(200)
# the parameter type each inferred name binds to; `Template` has no parameter type of its own
PG_TYPES: Final[dict[str, str]] = {
    "bool": "bool",
    "int": "int8",
    "float": "float8",
    "str": "text",
    "bytes": "bytea",
    "None": "text",
    "Decimal": "numeric",
    "UUID": "uuid",
    "date": "date",
    "datetime": "timestamp",
    "timedelta": "interval",
}


def pg_type(name: str) -> str | None:
    if name.startswith("list[") and name.endswith("]"):
        inner = pg_type(name.removeprefix("list[").removesuffix("]"))
        return None if inner is None else f"{inner}[]"
    return PG_TYPES.get(name)


def normalise(value: object) -> object:
    # an array parameter reads back as a list, whatever sequence was bound
    return [normalise(item) for item in value] if isinstance(value, list | tuple) else value


def test_inferred_types_encode_with_the_inferred_branch(schema: str) -> None:
    bound: list[tuple[str, object]] = []
    for seed in SEEDS:
        rng = seeded(seed)
        case = direct(rng) if seed % 3 == 0 else build(rng)
        name = shown(infer(case.source))
        if name is None or (sql_type := pg_type(name)) is None:
            continue
        bound.append((sql_type, run(case.source)))

    async def round_trip() -> list[tuple[str, object, object]]:
        conn = await connect(schema)
        try:
            found: list[tuple[str, object, object]] = []
            for sql_type, value in bound:
                rows = await conn._impl._fetch(
                    f"SELECT $1::{sql_type}", [value], None, scalar=True, limit_one=True, arity=1
                )
                found.append((sql_type, value, rows[0]))
            return found
        finally:
            await conn.close()

    results = asyncio.run(round_trip())
    # every kind the table maps is reached, so a branch cannot go untested
    assert {runtime_shown(value) for _, value, _ in results} >= set(PG_TYPES)
    for sql_type, value, back in results:
        # bool is not read as int and datetime is not read as date, so the value keeps its inferred type
        assert runtime_shown(back) == runtime_shown(normalise(value)), (sql_type, value, back)
        assert normalise(back) == normalise(value), (sql_type, value, back)

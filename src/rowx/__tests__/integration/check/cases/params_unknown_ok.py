import uuid
from decimal import Decimal

from rowx import query


def run(value, name: str | None, count: int) -> None:
    query[int](t'SELECT {value}::int AS "n!"')
    query[int](t'SELECT {count}::int AS "n!"')
    query[str | None](t'SELECT {name}::text AS "s?"')
    query[int | None](t'SELECT {None}::int AS "n?"')
    query[Decimal](t'SELECT {42}::numeric AS "n!"')
    query[uuid.UUID](t'SELECT {"c3d4bd56-0f3b-4bd9-8a3c-6b0a0d9d3a11"}::uuid AS "u!"')
    query[float](t'SELECT {1}::float8 AS "n!"')

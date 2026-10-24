import datetime as dt
import uuid
from decimal import Decimal

from rowx import query


def run() -> None:
    query[str](t'SELECT 1 AS "n!"')  # error: [type-mismatch]
    query[bool](t'SELECT 1 AS "n!"')  # error: [type-mismatch]
    query[int](t'SELECT true AS "b!"')  # error: [type-mismatch]
    query[float](t'SELECT 1.5::numeric AS "n!"')  # error: [type-mismatch]
    query[Decimal](t'SELECT 1.5::numeric AS "n!"')
    query[dt.date](t'SELECT now() AS "n!"')  # error: [type-mismatch]
    query[uuid.UUID](t'SELECT gen_random_uuid() AS "u!"')
    query[list[str]](t'SELECT ARRAY[1, 2] AS "a!"')  # error: [type-mismatch]
    query[list[int | None]](t'SELECT ARRAY[1, 2] AS "a!"')
    query[int](t'SELECT c_json AS "j!" FROM all_types WHERE c_json IS NOT NULL')
    query[tuple[None]](t'SELECT 1 AS "n!"')  # error: [type-mismatch]

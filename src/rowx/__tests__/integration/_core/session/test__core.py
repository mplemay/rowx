import asyncio
import datetime as dt
from decimal import Decimal
from typing import Any, Final

import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from rowx.postgres import connect

pytestmark = [pytest.mark.integration, pytest.mark.db]

# one value per setting that changes how the server parses or prints text
SETTINGS: Final[list[tuple[str, str]]] = [
    ("DateStyle", "ISO, YMD"),
    ("DateStyle", "SQL, DMY"),
    ("DateStyle", "German"),
    ("DateStyle", "Postgres, MDY"),
    ("IntervalStyle", "sql_standard"),
    ("IntervalStyle", "postgres_verbose"),
    ("IntervalStyle", "iso_8601"),
    ("TimeZone", "UTC"),
    ("TimeZone", "Asia/Kolkata"),
    ("TimeZone", "America/Los_Angeles"),
    ("extra_float_digits", "-15"),
    ("extra_float_digits", "0"),
    ("extra_float_digits", "3"),
]

PLUS_530: Final = dt.timezone(dt.timedelta(hours=5, minutes=30))
MINUS_8: Final = dt.timezone(dt.timedelta(hours=-8))

# SQL type -> value sent as text; the numeric, timetz, json, enum and array codecs all travel as text
TEXT_PARAMS: Final[list[tuple[str, Any]]] = [
    ("numeric", Decimal("-1234.5678")),
    ("numeric", Decimal("NaN")),
    ("numeric", Decimal("1E+30")),
    ("timetz", dt.time(1, 2, 3, 4, tzinfo=PLUS_530)),
    ("timetz", dt.time(23, 59, 59, tzinfo=MINUS_8)),
    ("json", {"d": "2024-01-02", "f": 0.1, "n": [1, None]}),
    ("jsonb", {"d": "2024-01-02", "f": 0.1, "n": [1, None]}),
    ("mood", "happy"),
    ("numeric[]", [Decimal("1.50"), None, Decimal("-0.001")]),
    ("timetz[]", [dt.time(1, 2, 3, tzinfo=PLUS_530), None]),
    ("mood[]", ["happy", "ok", None]),
    ("jsonb[]", [{"a": 1}, None]),
    ("float8[]", [0.1, 1e-320, None]),
]

# binary parameters are not affected by the settings, so they act as a control
BINARY_PARAMS: Final[list[tuple[str, Any]]] = [
    ("interval", dt.timedelta(days=1, hours=2, microseconds=5)),
    ("timestamptz", dt.datetime(2024, 1, 2, 3, 4, 5, tzinfo=dt.UTC)),
    ("timestamp", dt.datetime(2024, 1, 2, 3, 4, 5)),  # ruff: ignore[call-datetime-without-tzinfo]
    ("date", dt.date(2024, 2, 29)),
    ("float8", 0.1),
]


def with_setting(dsn: str, name: str, value: str) -> str:
    options = f"{conninfo_to_dict(dsn).get('options', '')} -c {name}={value.replace(' ', chr(92) + ' ')}"
    return make_conninfo(dsn, options=options)


def fetch_scalar(dsn: str, sql: str, value: object) -> object:
    async def run() -> object:
        conn = await connect(dsn)
        try:
            rows = await conn._impl._fetch(sql, [value], None, scalar=True, limit_one=True, arity=1)
        finally:
            await conn.close()
        return rows[0]

    return asyncio.run(run())


def same(left: object, right: object) -> bool:
    if isinstance(left, Decimal) and left.is_nan():
        return isinstance(right, Decimal) and right.is_nan()
    return left == right


@pytest.mark.parametrize(("name", "setting"), SETTINGS)
@pytest.mark.parametrize(("sql_type", "value"), [*TEXT_PARAMS, *BINARY_PARAMS], ids=lambda v: str(v)[:24])
def test_parameter_survives_session_settings(
    schema: str, name: str, setting: str, sql_type: str, value: object
) -> None:
    dsn = with_setting(schema, name, setting)
    # results come back in binary, so the value read back is the one the server parsed from the parameter
    assert same(fetch_scalar(dsn, f"SELECT $1::{sql_type}", value), value)

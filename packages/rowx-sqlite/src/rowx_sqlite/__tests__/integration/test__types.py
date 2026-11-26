import math
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, override
from uuid import UUID, uuid4

import pytest
from rowx_core.errors import DecodeError, EncodeError

from rowx import query

if TYPE_CHECKING:
    from rowx_sqlite import Connection


type Thirteen = tuple[
    object, object, object, object, object, object, object, object, object, object, object, object, object
]


async def store(conn: Connection, decl: str, value: object) -> None:
    await query(t"DROP TABLE IF EXISTS v").execute(conn)
    await conn._impl._execute(f"CREATE TABLE v (a {decl})", [])
    await query(t"INSERT INTO v VALUES ({value})").execute(conn)


async def stored_class(conn: Connection) -> str:
    return await query[str](t"SELECT typeof(a) FROM v").one(conn)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, 1, -1, 2**63 - 1, -(2**63)])
async def test_integer_round_trips(conn: Connection, value: int) -> None:
    await store(conn, "INTEGER", value)
    result = await query[int](t"SELECT a FROM v").one(conn)
    assert result == value
    assert type(result) is int


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [2**63, -(2**63) - 1, 10**30])
async def test_an_int_outside_i64_is_an_encode_error(conn: Connection, value: int) -> None:
    await query(t"CREATE TABLE v (a INTEGER)").execute(conn)
    with pytest.raises(EncodeError, match="INTEGER range"):
        await query(t"INSERT INTO v VALUES ({value})").execute(conn)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0.0, 1.5, -2.25e300, math.inf, -math.inf])
async def test_real_round_trips(conn: Connection, value: float) -> None:
    await store(conn, "REAL", value)
    assert await query[float](t"SELECT a FROM v").one(conn) == value


@pytest.mark.asyncio
async def test_nan_is_rejected_on_bind(conn: Connection) -> None:
    await query(t"CREATE TABLE v (a REAL)").execute(conn)
    nan = math.nan
    with pytest.raises(EncodeError, match="NaN"):
        await query(t"INSERT INTO v VALUES ({nan})").execute(conn)


@pytest.mark.asyncio
async def test_a_whole_real_reads_back_as_a_float(conn: Connection) -> None:
    # REAL affinity stores 3.0 as the integer 3; the declared type still reads it as a float
    await store(conn, "REAL", 3.0)
    result = await query[float](t"SELECT a FROM v").one(conn)
    assert result == pytest.approx(3.0)
    assert type(result) is float


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", "hello", "héllo ✓ 日本語", "emoji 😀", "quote ' \" and \\"])
async def test_text_round_trips(conn: Connection, value: str) -> None:
    await store(conn, "TEXT", value)
    assert await query[str](t"SELECT a FROM v").one(conn) == value


@pytest.mark.asyncio
async def test_text_with_a_nul_is_an_encode_error(conn: Connection) -> None:
    await query(t"CREATE TABLE v (a TEXT)").execute(conn)
    value = "a\0b"
    with pytest.raises(EncodeError, match="NUL"):
        await query(t"INSERT INTO v VALUES ({value})").execute(conn)


@pytest.mark.asyncio
async def test_a_lone_surrogate_cannot_be_bound(conn: Connection) -> None:
    await query(t"CREATE TABLE v (a TEXT)").execute(conn)
    value = "bad \ud800"
    with pytest.raises(EncodeError, match="surrogate"):
        await query(t"INSERT INTO v VALUES ({value})").execute(conn)


@pytest.mark.asyncio
async def test_invalid_utf8_text_raises_on_read(conn: Connection) -> None:
    await query(t"CREATE TABLE v (a TEXT)").execute(conn)
    await query(t"INSERT INTO v VALUES (CAST(x'ff' AS TEXT))").execute(conn)
    with pytest.raises(DecodeError, match="UTF-8"):
        await query[str](t"SELECT a FROM v").one(conn)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [b"", b"\x00\x01\xff", b"x" * 10_000])
async def test_blob_round_trips(conn: Connection, value: bytes) -> None:
    await store(conn, "BLOB", value)
    result = await query[bytes](t"SELECT a FROM v").one(conn)
    assert result == value
    assert type(result) is bytes


@pytest.mark.asyncio
async def test_bytearray_and_memoryview_bind_as_blobs(conn: Connection) -> None:
    await query(t"CREATE TABLE v (a BLOB)").execute(conn)
    array = bytearray(b"abc")
    view = memoryview(b"def")
    await query(t"INSERT INTO v VALUES ({array}), ({view})").execute(conn)
    assert await query[bytes](t"SELECT a FROM v ORDER BY rowid").all(conn) == [b"abc", b"def"]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, False])
async def test_boolean_round_trips(conn: Connection, value: bool) -> None:  # ruff: ignore[boolean-type-hint-positional-argument]
    await store(conn, "BOOLEAN", value)
    result = await query[bool](t"SELECT a FROM v").one(conn)
    assert result is value
    assert await stored_class(conn) == "integer"


@pytest.mark.asyncio
async def test_a_boolean_column_rejects_a_stored_value_that_is_not_zero_or_one(conn: Connection) -> None:
    await store(conn, "BOOLEAN", 2)
    with pytest.raises(DecodeError):
        await query[bool](t"SELECT a FROM v").one(conn)


@pytest.mark.asyncio
async def test_naive_datetime_round_trips(conn: Connection) -> None:
    value = datetime(2024, 5, 6, 7, 8, 9, 123456)  # ruff: ignore[call-datetime-without-tzinfo]
    await store(conn, "DATETIME", value)
    result = await query[datetime](t"SELECT a FROM v").one(conn)
    assert result == value
    assert result.tzinfo is None
    assert await query[str](t"SELECT CAST(a AS TEXT) FROM v").one(conn) == "2024-05-06 07:08:09.123456"


@pytest.mark.asyncio
async def test_aware_datetime_is_stored_as_utc_and_returned_as_utc(conn: Connection) -> None:
    value = datetime(2024, 5, 6, 7, 8, 9, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    await store(conn, "TIMESTAMPTZ", value)
    result = await query[datetime](t"SELECT a FROM v").one(conn)
    assert result == value
    assert result.utcoffset() == timedelta(0)
    assert result.tzinfo is UTC
    assert await query[str](t"SELECT CAST(a AS TEXT) FROM v").one(conn) == "2024-05-06 01:38:09+00:00"


@pytest.mark.asyncio
async def test_a_fractional_second_utc_offset_is_an_encode_error(conn: Connection) -> None:
    await query(t"CREATE TABLE v (a TIMESTAMPTZ)").execute(conn)
    value = datetime(2024, 1, 1, tzinfo=timezone(timedelta(seconds=0.5)))
    with pytest.raises(EncodeError, match="fraction"):
        await query(t"INSERT INTO v VALUES ({value})").execute(conn)


@pytest.mark.asyncio
async def test_a_stored_naive_datetime_cannot_be_read_as_aware_by_accident(conn: Connection) -> None:
    # a naive value stored in a TIMESTAMPTZ column has no zone to convert from, so reading it is an error
    value = datetime(2024, 5, 6, 7, 8, 9)  # ruff: ignore[call-datetime-without-tzinfo]
    await store(conn, "TIMESTAMPTZ", value)
    with pytest.raises(DecodeError):
        await query[datetime](t"SELECT a FROM v").one(conn)


@pytest.mark.asyncio
async def test_date_round_trips(conn: Connection) -> None:
    value = date(2024, 2, 29)
    await store(conn, "DATE", value)
    result = await query[date](t"SELECT a FROM v").one(conn)
    assert result == value
    assert type(result) is date
    assert await query[str](t"SELECT CAST(a AS TEXT) FROM v").one(conn) == "2024-02-29"


@pytest.mark.asyncio
async def test_time_round_trips(conn: Connection) -> None:
    value = time(23, 59, 58, 500)
    await store(conn, "TIME", value)
    assert await query[time](t"SELECT a FROM v").one(conn) == value


@pytest.mark.asyncio
async def test_an_aware_time_is_an_encode_error(conn: Connection) -> None:
    await query(t"CREATE TABLE v (a TIME)").execute(conn)
    value = time(1, 2, 3, tzinfo=UTC)
    with pytest.raises(EncodeError, match="aware time"):
        await query(t"INSERT INTO v VALUES ({value})").execute(conn)


@pytest.mark.asyncio
async def test_decimal_round_trips_through_text(conn: Connection) -> None:
    value = Decimal("12345.5")
    await store(conn, "DECIMAL(10, 2)", value)
    result = await query[Decimal](t"SELECT a FROM v").one(conn)
    assert result == value
    assert type(result) is Decimal


@pytest.mark.asyncio
async def test_an_exact_decimal_needs_text_storage(conn: Connection) -> None:
    # NUMERIC affinity turns '1.50' into the REAL 1.5, which is why the docs call a DECIMAL column lossy
    await store(conn, "DECIMAL", Decimal("1.50"))
    assert await stored_class(conn) == "real"
    assert await query[Decimal](t"SELECT a FROM v").one(conn) == Decimal("1.5")


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
async def test_a_non_finite_decimal_is_an_encode_error(conn: Connection, value: Decimal) -> None:
    await query(t"CREATE TABLE v (a DECIMAL)").execute(conn)
    with pytest.raises(EncodeError, match="finite"):
        await query(t"INSERT INTO v VALUES ({value})").execute(conn)


@pytest.mark.asyncio
async def test_uuid_round_trips_as_lowercase_text(conn: Connection) -> None:
    value = uuid4()
    await store(conn, "UUID", value)
    result = await query[UUID](t"SELECT a FROM v").one(conn)
    assert result == value
    assert await query[str](t"SELECT CAST(a AS TEXT) FROM v").one(conn) == str(value)


@pytest.mark.asyncio
async def test_json_round_trips(conn: Connection) -> None:
    value = {"a": [1, 2.5, None, True], "b": {"c": "é"}}
    await store(conn, "JSON", value)
    assert await query[object](t"SELECT a FROM v").one(conn) == value
    assert (
        await query[str](t"SELECT CAST(a AS TEXT) FROM v").one(conn)
        == '{"a": [1, 2.5, null, true], "b": {"c": "\\u00e9"}}'
    )


@pytest.mark.asyncio
async def test_a_json_list_and_tuple_bind_as_json(conn: Connection) -> None:
    await query(t"CREATE TABLE v (a JSON)").execute(conn)
    listed = [1, 2]
    tupled = (3, 4)
    await query(t"INSERT INTO v VALUES ({listed}), ({tupled})").execute(conn)
    assert await query[object](t"SELECT a FROM v ORDER BY rowid").all(conn) == [[1, 2], [3, 4]]


@pytest.mark.asyncio
async def test_a_value_json_cannot_serialise_is_an_encode_error(conn: Connection) -> None:
    await query(t"CREATE TABLE v (a JSON)").execute(conn)
    for value in ({"a": {1, 2}}, {"a": math.nan}):
        with pytest.raises(EncodeError, match="JSON"):
            await query(t"INSERT INTO v VALUES ({value})").execute(conn)


@pytest.mark.asyncio
async def test_json_that_does_not_parse_is_a_decode_error(conn: Connection) -> None:
    await store(conn, "JSON", "{not json")
    with pytest.raises(DecodeError, match="JSON"):
        await query[object](t"SELECT a FROM v").one(conn)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["NaN", "[NaN]", '{"a": Infinity}', "[-Infinity]"])
async def test_a_non_finite_json_constant_is_a_decode_error(conn: Connection, text: str) -> None:
    # the encoder never writes these (`allow_nan=False`), so the decoder does not accept them either
    await store(conn, "JSON", text)
    with pytest.raises(DecodeError, match="not a JSON number"):
        await query[object](t"SELECT a FROM v").one(conn)


# `bytes` names the property inside LyingUuid, so its return type is spelled outside the class
type Raw = bytes


class LyingDecimal(Decimal):
    @override
    def __str__(self) -> str:
        return "999"


class LyingUuid(UUID):
    @property
    @override
    def bytes(self) -> Raw:
        return b"\xff" * 16


class LyingDatetime(datetime):
    @property
    @override
    def year(self) -> int:
        return 1999

    @override
    def utcoffset(self) -> timedelta | None:
        return timedelta(hours=5)


class LyingDate(date):
    @property
    @override
    def day(self) -> int:
        return 9


@pytest.mark.asyncio
async def test_a_subclass_cannot_change_the_bound_value(conn: Connection) -> None:
    await query(t"CREATE TABLE v (a TEXT)").execute(conn)
    number = LyingDecimal("12.5")
    ident = LyingUuid("12345678-1234-5678-1234-567812345678")
    stamp = LyingDatetime(2024, 1, 2, 3, 4, 5)
    day = LyingDate(2024, 1, 2)
    await query(t"INSERT INTO v VALUES ({number}), ({ident}), ({stamp}), ({day})").execute(conn)
    assert await query[str](t"SELECT a FROM v ORDER BY rowid").all(conn) == [
        "12.5",
        "12345678-1234-5678-1234-567812345678",
        "2024-01-02 03:04:05",
        "2024-01-02",
    ]


@pytest.mark.asyncio
async def test_an_untyped_column_decodes_by_storage_class(conn: Connection) -> None:
    await query(t"CREATE TABLE v (a)").execute(conn)
    values: list[object] = [7, 1.5, "text", b"\x00\x01", None]
    for value in values:
        await query(t"INSERT INTO v VALUES ({value})").execute(conn)
    rows = await query[object](t"SELECT a FROM v ORDER BY rowid").all(conn)
    assert rows == values
    assert [type(row) for row in rows] == [int, float, str, bytes, type(None)]


@pytest.mark.asyncio
async def test_a_strict_any_column_decodes_by_storage_class(conn: Connection) -> None:
    await query(t"CREATE TABLE v (a ANY) STRICT").execute(conn)
    values: list[object] = [7, 1.5, "text", b"\x00"]
    for value in values:
        await query(t"INSERT INTO v VALUES ({value})").execute(conn)
    assert await query[object](t"SELECT a FROM v ORDER BY rowid").all(conn) == values


@pytest.mark.asyncio
async def test_null_round_trips_for_every_family(conn: Connection) -> None:
    await query(
        t"CREATE TABLE v (i INTEGER, r REAL, t TEXT, b BLOB, o BOOLEAN, dt DATETIME, tz TIMESTAMPTZ, d DATE, tm TIME, n DECIMAL, u UUID, j JSON, x)",
    ).execute(conn)
    await query(t"INSERT INTO v DEFAULT VALUES").execute(conn)
    row = await query[Thirteen](t"SELECT i, r, t, b, o, dt, tz, d, tm, n, u, j, x FROM v").one(conn)
    assert row == (None,) * 13


@pytest.mark.asyncio
async def test_none_binds_as_null(conn: Connection) -> None:
    await query(t"CREATE TABLE v (a INTEGER)").execute(conn)
    value = None
    await query(t"INSERT INTO v VALUES ({value})").execute(conn)
    assert await query[int | None](t"SELECT a FROM v").one(conn) is None

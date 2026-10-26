import asyncio
from typing import TYPE_CHECKING, Any, Final

import pytest

from rowx import query
from rowx.errors import DatabaseError, EncodeError
from rowx.postgres import Connection, connect

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

pytestmark = [pytest.mark.integration, pytest.mark.db]

STRING_DATA_LENGTH_MISMATCH: Final[str] = "22026"
# around the byte boundary, and past 64 bits where no integer holds the value
LENGTHS: Final[list[int]] = [1, 7, 8, 9, 15, 16, 17, 63, 64, 65, 129]


def run_with_conn[T](dsn: str, body: Callable[[Connection], Awaitable[T]]) -> T:
    async def run() -> T:
        async with await connect(dsn) as conn:
            return await body(conn)

    return asyncio.run(run())


def read(dsn: str, sql: str, params: list[object] | None = None) -> Any:  # ruff: ignore[any-type]
    async def run(conn: Connection) -> Any:  # ruff: ignore[any-type]
        return (await conn._impl._fetch(sql, params or [], None, scalar=True, limit_one=True, arity=1))[0]

    return run_with_conn(dsn, run)


def pattern(length: int) -> str:
    # alternating, starting and ending with 1, so a swapped bit order or a lost last bit shows
    return ("10" * length)[: length - 1] + "1" if length > 1 else "1"


@pytest.mark.parametrize("length", LENGTHS)
def test_bit_decodes_to_its_digits(schema: str, length: int) -> None:
    bits = pattern(length)
    assert read(schema, f"SELECT B'{bits}'::bit({length})") == bits


@pytest.mark.parametrize("length", [0, *LENGTHS])
def test_varbit_decodes_to_its_digits(schema: str, length: int) -> None:
    bits = pattern(length) if length else ""
    assert read(schema, f"SELECT '{bits}'::varbit") == bits


def test_bit_order_is_high_bit_first(schema: str) -> None:
    assert read(schema, "SELECT B'10000000'") == "10000000"
    assert read(schema, "SELECT B'00000001'") == "00000001"
    assert read(schema, "SELECT B'1000000000000001'") == "1000000000000001"


@pytest.mark.parametrize("length", LENGTHS)
def test_bit_round_trips(schema: str, length: int) -> None:
    bits = pattern(length)
    assert read(schema, f"SELECT $1::bit({length})", [bits]) == bits
    assert read(schema, "SELECT $1::varbit", [bits]) == bits


def test_empty_varbit_round_trips(schema: str) -> None:
    empty = read(schema, "SELECT $1::varbit", [""])
    assert isinstance(empty, str)
    assert len(empty) == 0
    assert read(schema, "SELECT length($1::varbit)", [""]) == 0


def test_all_zero_and_all_one_strings_keep_their_length(schema: str) -> None:
    for bits in ("0" * 9, "1" * 9, "0" * 70, "1" * 70):
        assert read(schema, "SELECT $1::varbit", [bits]) == bits


def test_server_sees_the_bits_we_send(schema: str) -> None:
    assert read(schema, "SELECT $1::varbit::text", ["101"]) == "101"
    assert read(schema, "SELECT get_bit($1::varbit, 0)", ["100000000"]) == 1
    assert read(schema, "SELECT get_bit($1::varbit, 8)", ["100000001"]) == 1
    assert read(schema, "SELECT bit_length($1::varbit)", ["1" * 70]) == 70


def test_bit_arrays_round_trip_with_null_elements(schema: str) -> None:
    assert read(schema, "SELECT '{101,NULL,000}'::bit(3)[]") == ["101", None, "000"]
    value = [["10101010101", None], ["1", ""]]
    assert read(schema, "SELECT $1::varbit[]", [value]) == value
    assert read(schema, "SELECT $1::bit(3)[]", [["101", None]]) == ["101", None]


def test_bit_is_read_through_a_query(schema: str) -> None:
    async def body(conn: Connection) -> tuple[str, str | None, list[str | None]]:
        one = await query[str](t"SELECT {'1011'}::varbit").one(conn)
        none = await query[str | None](t"SELECT NULL::bit(3)").one(conn)
        many = await query[list[str | None]](t"SELECT ARRAY[{'11'}, NULL]::varbit[]").one(conn)
        return one, none, many

    assert run_with_conn(schema, body) == ("1011", None, ["11", None])


# a parameter is typed `bit`, so only the server's assignment cast sees the declared length
@pytest.mark.parametrize("bits", ["101", "1010101010", ""])
def test_bit_n_parameter_of_the_wrong_length_is_rejected_by_the_server(schema: str, bits: str) -> None:
    async def body(conn: Connection) -> None:
        await conn._impl._execute("CREATE TEMP TABLE bit_fixed (b bit(8))", [])
        await conn._impl._execute("INSERT INTO bit_fixed VALUES ($1)", [bits])

    with pytest.raises(DatabaseError) as info:
        run_with_conn(schema, body)
    assert info.value.sqlstate == STRING_DATA_LENGTH_MISMATCH


def test_bit_n_parameter_of_the_right_length_is_accepted(schema: str) -> None:
    async def body(conn: Connection) -> object:
        await conn._impl._execute("CREATE TEMP TABLE bit_fixed (b bit(8))", [])
        await conn._impl._execute("INSERT INTO bit_fixed VALUES ($1)", ["10100101"])
        return (await conn._impl._fetch("SELECT b FROM bit_fixed", [], None, scalar=True, limit_one=True, arity=1))[0]

    assert run_with_conn(schema, body) == "10100101"


@pytest.mark.parametrize("bits", ["102", "1 0", " 1", "x", "\uff11"])
def test_other_characters_are_an_encode_error(schema: str, bits: str) -> None:
    with pytest.raises(EncodeError, match="0` and `1"):
        read(schema, "SELECT $1::varbit", [bits])


def test_bit_takes_only_str(schema: str) -> None:
    for value in (5, b"101", [1, 0, 1]):
        with pytest.raises(EncodeError):
            read(schema, "SELECT $1::varbit", [value])

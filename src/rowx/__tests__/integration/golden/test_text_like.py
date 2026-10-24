import asyncio
from array import array
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest

import rowx
from rowx import query
from rowx.errors import EncodeError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

pytestmark = [pytest.mark.integration, pytest.mark.db]


def run_with_conn[T](dsn: str, body: Callable[[rowx.Connection], Awaitable[T]]) -> T:
    async def run() -> T:
        async with await rowx.connect(dsn) as conn:
            return await body(conn)

    return asyncio.run(run())


@pytest.mark.parametrize("value", ["", "hello", "héllo wörld ☃ 😀", "a" * 10_000])
def test_text_round_trip(schema: str, value: str) -> None:
    assert run_with_conn(schema, query[str](t"SELECT {value}::text").fetch_one) == value


def test_nul_str_is_encode_error(schema: str) -> None:
    value = "a\0b"
    with pytest.raises(EncodeError, match=r"\$1.*NUL"):
        run_with_conn(schema, query[str](t"SELECT {value}::text").fetch_one)


def test_bpchar_and_name(schema: str) -> None:
    q = query[tuple[str, str]](t"SELECT 'ab'::char(4), 'nm'::name")
    assert run_with_conn(schema, q.fetch_one) == ("ab  ", "nm")


def test_char_round_trip(schema: str) -> None:
    value = "x"
    assert run_with_conn(schema, query[str](t'SELECT {value}::"char"').fetch_one) == value


def test_char_multi_byte_is_encode_error(schema: str) -> None:
    value = "é"
    with pytest.raises(EncodeError, match=r"\$1.*char"):
        run_with_conn(schema, query[str](t'SELECT {value}::"char"').fetch_one)


@pytest.mark.parametrize(
    "value",
    [b"", b"abc", b"a\x00b", bytes(range(256))],
)
def test_bytea_round_trip(schema: str, value: bytes) -> None:
    assert run_with_conn(schema, query[bytes](t"SELECT {value}::bytea").fetch_one) == value


def test_bytea_accepts_bytes_likes(schema: str) -> None:
    for value in (bytearray(b"x\x00y"), memoryview(b"x\x00y")):
        got = run_with_conn(schema, query[bytes](t"SELECT {value}::bytea").fetch_one)
        assert got == b"x\x00y"


@pytest.mark.parametrize("value", [memoryview(array("b", [-1, 0, 1])), memoryview(array("I", [123]))])
def test_bytea_takes_raw_bytes_of_any_memoryview_format(schema: str, value: memoryview) -> None:
    got = run_with_conn(schema, query[bytes](t"SELECT {value}::bytea").fetch_one)
    assert got == value.tobytes()


def test_bytes_to_int4_is_encode_error(schema: str) -> None:
    value = b"1"
    with pytest.raises(EncodeError, match=r"\$1.*bytes.*int4"):
        run_with_conn(schema, query[int](t"SELECT {value}::int4").fetch_one)


def test_uuid_round_trip(schema: str) -> None:
    value = UUID("12345678-1234-5678-1234-567812345678")
    got = run_with_conn(schema, query[UUID](t"SELECT {value}::uuid").fetch_one)
    assert got == value
    assert isinstance(got, UUID)


def test_uuid_from_str(schema: str) -> None:
    value = "12345678-1234-5678-1234-567812345678"
    got = run_with_conn(schema, query[UUID](t"SELECT {value}::uuid").fetch_one)
    assert got == UUID(value)


@pytest.mark.parametrize("value", [None, 1, "s", [1, "a", None], {"a": {"b": [1.5, True]}}])
def test_jsonb_round_trip(schema: str, value: Any) -> None:  # ruff: ignore[any-type]
    got = run_with_conn(schema, query[Any](t"SELECT {value}::jsonb").fetch_one)
    assert got == value


@pytest.mark.parametrize("value", [1, "s", [1, 2], {"a": 1}])
def test_json_round_trip(schema: str, value: Any) -> None:  # ruff: ignore[any-type]
    got = run_with_conn(schema, query[Any](t"SELECT {value}::json").fetch_one)
    assert got == value


def test_str_binds_as_json_string(schema: str) -> None:
    value = "[1]"
    q = query[tuple[Any, str]](t"SELECT {value}::jsonb, jsonb_typeof({value}::jsonb)")
    assert run_with_conn(schema, q.fetch_one) == ("[1]", "string")


def test_raw_json_text_via_cast(schema: str) -> None:
    text = "[1]"
    q = query[tuple[Any, str]](t"SELECT {text}::text::jsonb, jsonb_typeof({text}::text::jsonb)")
    assert run_with_conn(schema, q.fetch_one) == ([1], "array")


def test_unserialisable_json_is_encode_error(schema: str) -> None:
    value = {1, 2}
    with pytest.raises(EncodeError, match=r"\$1.*JSON"):
        run_with_conn(schema, query[Any](t"SELECT {value}::jsonb").fetch_one)


def test_enum_round_trip(schema: str) -> None:
    value = "happy"
    got = run_with_conn(schema, query[str](t"SELECT {value}::mood").fetch_one)
    assert got == value


def test_enum_bad_label_is_database_error(schema: str) -> None:
    value = "furious"
    with pytest.raises(rowx.errors.DatabaseError):
        run_with_conn(schema, query[str](t"SELECT {value}::mood").fetch_one)


def test_citext_round_trip(schema: str) -> None:
    value = "MiXed"
    q = query[tuple[str, bool]](t"SELECT {value}::public.citext, {value}::public.citext OPERATOR(public.=) 'mixed'")
    assert run_with_conn(schema, q.fetch_one) == (value, True)


def test_domain_column_decodes_as_base_type(schema: str) -> None:
    q = query[int](t"SELECT 5::positive_int")
    assert run_with_conn(schema, q.fetch_one) == 5


def test_domain_param_encodes(schema: str) -> None:
    value = 7
    q = query[int](t"SELECT ({value}::positive_int)::int")
    assert run_with_conn(schema, q.fetch_one) == 7

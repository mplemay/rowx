import asyncio
from typing import TYPE_CHECKING, Any, Final

import pytest

from rowx.errors import DatabaseError, EncodeError
from rowx.postgres import Connection, Cube, connect

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

pytestmark = [pytest.mark.integration, pytest.mark.db]

INVALID_TEXT_REPRESENTATION: Final[str] = "22P02"

# the extensions live in public, which the test search_path leaves out
VALUES: Final[dict[str, Any]] = {
    "public.hstore": {"a": "1", "b": None, "c d": "", "é": "ü"},
    "public.ltree": "Top.Science.Astronomy",
    "public.lquery": "Top.*{1,2}.Astronomy",
    "public.cube": Cube((-1.5, 2.0), (3.0, 1e10)),
    "public.citext": "MiXed",
}


def run_with_conn[T](dsn: str, body: Callable[[Connection], Awaitable[T]]) -> T:
    async def run() -> T:
        async with await connect(dsn) as conn:
            return await body(conn)

    return asyncio.run(run())


def read(dsn: str, sql: str, params: list[object] | None = None) -> Any:  # ruff: ignore[any-type]
    async def run(conn: Connection) -> Any:  # ruff: ignore[any-type]
        return (await conn._impl._fetch(sql, params or [], None, scalar=True, limit_one=True, arity=1))[0]

    return run_with_conn(dsn, run)


@pytest.mark.parametrize("sql_type", sorted(VALUES))
def test_extension_value_round_trips(schema: str, sql_type: str) -> None:
    assert read(schema, f"SELECT $1::{sql_type}", [VALUES[sql_type]]) == VALUES[sql_type]


@pytest.mark.parametrize("sql_type", sorted(VALUES))
def test_extension_array_decodes_and_encodes_with_null_elements_and_dimensions(schema: str, sql_type: str) -> None:
    value = VALUES[sql_type]
    assert read(schema, f"SELECT $1::{sql_type}[]", [[value, None, value]]) == [value, None, value]
    matrix = [[value, None], [value, value]]
    assert read(schema, f"SELECT $1::{sql_type}[]", [matrix]) == matrix
    assert read(schema, f"SELECT '{{}}'::{sql_type}[]") == []
    assert read(schema, f"SELECT ARRAY[NULL]::{sql_type}[]") == [None]


@pytest.mark.parametrize("sql_type", sorted(VALUES))
def test_extension_is_read_back_as_null_for_null(schema: str, sql_type: str) -> None:
    assert read(schema, f"SELECT $1::{sql_type}", [None]) is None


def test_citext_array_binds_a_list_of_str_and_reads_back_str(schema: str) -> None:
    # the server treats citext[] as its own type (sqlx notes it is not text[] for parameters), so the element
    # type must come from the parameter, not from the Python values
    assert read(schema, "SELECT $1::public.citext[]", [["a", "B", None]]) == ["a", "B", None]
    assert read(schema, "SELECT ($1::public.citext[])[1] OPERATOR(public.=) 'A'", [["a"]]) is True
    assert read(schema, "SELECT $1::public.citext[] = ARRAY['A']::public.citext[]", [["a"]]) is True


def test_citext_array_is_not_accepted_for_a_text_array_parameter_by_the_server(schema: str) -> None:
    # not a rowx rule: it documents why the checker and the encoder follow the parameter's own type
    with pytest.raises(DatabaseError, match="operator does not exist"):
        read(schema, "SELECT $1::text[] = ARRAY['a']::public.citext[]", [["a"]])


# --- hstore ---


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        ("", {}),
        ("a=>1", {"a": "1"}),
        ("a=>NULL", {"a": None}),
        ('a=>"NULL"', {"a": "NULL"}),
        ('"a b"=>"c, d"', {"a b": "c, d"}),
        ('k=>""', {"k": ""}),
        ('"a=>b"=>"c=>d"', {"a=>b": "c=>d"}),
        ("a=>1, a=>2", {"a": "1"}),
        ('"quote\\"d"=>"back\\\\slash"', {'quote"d': "back\\slash"}),
    ],
)
def test_hstore_decodes_from_server_literals(schema: str, literal: str, expected: dict[str, str | None]) -> None:
    assert read(schema, f"SELECT '{literal}'::public.hstore") == expected


def test_hstore_empty_value_is_not_null(schema: str) -> None:
    value = read(schema, "SELECT $1::public.hstore", [{"empty": "", "null": None}])
    assert value == {"empty": "", "null": None}
    assert value["empty"] is not None


def test_hstore_encoded_value_means_the_same_to_the_server(schema: str) -> None:
    value = {"k": "v", "e": "", "n": None}
    assert read(schema, "SELECT $1::public.hstore OPERATOR(public.->) 'k'", [value]) == "v"
    assert len(read(schema, "SELECT $1::public.hstore OPERATOR(public.->) 'e'", [value])) == 0
    assert read(schema, "SELECT public.exist($1::public.hstore, 'n')", [value]) is True
    assert read(schema, "SELECT ($1::public.hstore OPERATOR(public.->) 'n') IS NULL", [value]) is True
    assert read(schema, "SELECT public.akeys($1::public.hstore)", [{"b": "1", "a": "2"}]) == ["a", "b"]


def test_hstore_null_key_is_encode_error(schema: str) -> None:
    with pytest.raises(EncodeError, match=r"\$1.*key cannot be NULL"):
        read(schema, "SELECT $1::public.hstore", [{None: "x"}])


@pytest.mark.parametrize("value", [{"a": 1}, {1: "a"}, {"a": b"x"}, {"a": ["x"]}, {("a",): "x"}])
def test_hstore_non_str_key_or_value_is_encode_error(schema: str, value: dict[Any, Any]) -> None:
    with pytest.raises(EncodeError, match=r"\$1.*hstore (key|value) must be a str"):
        read(schema, "SELECT $1::public.hstore", [value])


@pytest.mark.parametrize("value", ["a=>1", ["a"], 1, b"a=>1"])
def test_hstore_only_takes_a_dict(schema: str, value: object) -> None:
    with pytest.raises(EncodeError, match=r"\$1"):
        read(schema, "SELECT $1::public.hstore", [value])


def test_hstore_nul_in_a_string_is_rejected_by_the_server(schema: str) -> None:
    with pytest.raises(DatabaseError):
        read(schema, "SELECT $1::public.hstore", [{"a\0b": "x"}])


def test_hstore_large_value_round_trips(schema: str) -> None:
    value = {f"key{index}": None if index % 3 == 0 else "v" * index for index in range(300)}
    assert read(schema, "SELECT $1::public.hstore", [value]) == value


# --- ltree and lquery ---


@pytest.mark.parametrize("sql_type", ["public.ltree", "public.lquery"])
def test_label_tree_nul_is_encode_error(schema: str, sql_type: str) -> None:
    with pytest.raises(EncodeError, match=r"\$1.*NUL"):
        read(schema, f"SELECT $1::{sql_type}", ["a\0b"])


@pytest.mark.parametrize("sql_type", ["public.ltree", "public.lquery"])
@pytest.mark.parametrize("value", [1, b"a", ["a"], {"a": "b"}, 1.5])
def test_label_tree_only_takes_a_str(schema: str, sql_type: str, value: object) -> None:
    with pytest.raises(EncodeError, match=r"\$1"):
        read(schema, f"SELECT $1::{sql_type}", [value])


@pytest.mark.parametrize("value", ["a b", "a..b", ".a", "a.", "a.*"])
def test_ltree_invalid_text_is_a_server_error(schema: str, value: str) -> None:
    with pytest.raises(DatabaseError) as info:
        read(schema, "SELECT $1::public.ltree", [value])
    assert info.value.sqlstate in {"42601", INVALID_TEXT_REPRESENTATION, "22023"}


def test_ltree_empty_path_round_trips(schema: str) -> None:
    assert len(read(schema, "SELECT $1::public.ltree", [""])) == 0
    assert len(read(schema, "SELECT ''::public.ltree")) == 0


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        ("a", "a"),
        ("a.b.c", "a.b.c"),
        ("A_1.b-2", "A_1.b-2"),
    ],
)
def test_ltree_decodes_from_server_literals(schema: str, literal: str, expected: str) -> None:
    assert read(schema, f"SELECT '{literal}'::public.ltree") == expected


def test_ltree_and_lquery_work_in_server_operators(schema: str) -> None:
    assert (
        read(schema, "SELECT $1::public.ltree OPERATOR(public.~) $2::public.lquery", ["Top.Science", "Top.*"]) is True
    )
    assert read(schema, "SELECT public.nlevel($1::public.ltree)", ["a.b.c"]) == 3


# --- cube ---


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        ("()", Cube((), ())),
        ("(1)", Cube((1.0,), (1.0,))),
        ("(1),(2)", Cube((1.0,), (2.0,))),
        ("(1,2)", Cube((1.0, 2.0), (1.0, 2.0))),
        ("(1,2),(3,4)", Cube((1.0, 2.0), (3.0, 4.0))),
        # the server keeps the corners in the order given, so `lower_left` is only the first corner written
        ("(3,4),(1,2)", Cube((3.0, 4.0), (1.0, 2.0))),
        ("(-1.5e3,0),(2,1e-3)", Cube((-1500.0, 0.0), (2.0, 0.001))),
    ],
)
def test_cube_decodes_from_server_literals(schema: str, literal: str, expected: Cube) -> None:
    assert read(schema, f"SELECT '{literal}'::public.cube") == expected


def test_cube_point_is_sent_as_a_point(schema: str) -> None:
    point = Cube((1.0, 2.0), (1.0, 2.0))
    assert read(schema, "SELECT public.cube_is_point($1::public.cube)", [point]) is True
    assert read(schema, "SELECT $1::public.cube::text", [point]) == "(1, 2)"
    # corners that differ anywhere make a box
    box = Cube((1.0, 2.0), (1.0, 3.0))
    assert read(schema, "SELECT public.cube_is_point($1::public.cube)", [box]) is False
    assert read(schema, "SELECT $1::public.cube::text", [box]) == "(1, 2),(1, 3)"


def test_cube_dimension_is_kept(schema: str) -> None:
    assert read(schema, "SELECT public.cube_dim($1::public.cube)", [Cube((1.0,) * 5, (2.0,) * 5)]) == 5
    assert read(schema, "SELECT public.cube_dim($1::public.cube)", [Cube((), ())]) == 0
    assert read(schema, "SELECT public.cube_dim($1::public.cube)", [Cube((1.0,) * 100, (1.0,) * 100)]) == 100


def test_cube_corners_of_different_lengths_are_encode_error(schema: str) -> None:
    with pytest.raises(EncodeError, match=r"\$1.*same number"):
        read(schema, "SELECT $1::public.cube", [Cube((1.0,), (1.0, 2.0))])


def test_cube_past_the_dimension_limit_is_encode_error(schema: str) -> None:
    with pytest.raises(EncodeError, match=r"\$1.*100"):
        read(schema, "SELECT $1::public.cube", [Cube((0.0,) * 101, (0.0,) * 101)])


@pytest.mark.parametrize("value", ["(1,2)", (1.0, 2.0), [1.0], 1.0, {"a": 1}])
def test_cube_only_takes_a_cube(schema: str, value: object) -> None:
    with pytest.raises(EncodeError, match=r"\$1"):
        read(schema, "SELECT $1::public.cube", [value])


def test_cube_with_a_non_number_coordinate_is_encode_error(schema: str) -> None:
    with pytest.raises(EncodeError, match=r"\$1.*not a number"):
        read(schema, "SELECT $1::public.cube", [Cube(("a",), (1.0,))])  # ty: ignore[invalid-argument-type]


def test_cube_takes_ints_and_keeps_special_floats(schema: str) -> None:
    assert read(schema, "SELECT $1::public.cube", [Cube((1,), (2,))]) == Cube((1.0,), (2.0,))
    assert read(schema, "SELECT $1::public.cube", [Cube((float("inf"),), (float("inf"),))]) == Cube(
        (float("inf"),), (float("inf"),)
    )
    assert read(schema, "SELECT $1::public.cube", [Cube((-0.0,), (0.0,))]) == Cube((-0.0,), (0.0,))


def test_cube_server_operators_see_the_value(schema: str) -> None:
    assert (
        read(
            schema,
            "SELECT $1::public.cube OPERATOR(public.&&) $2::public.cube",
            [Cube((0.0,), (2.0,)), Cube((1.0,), (3.0,))],
        )
        is True
    )
    distance = read(
        schema,
        "SELECT public.cube_distance($1::public.cube, $2::public.cube)",
        [Cube((0.0, 0.0), (0.0, 0.0)), Cube((3.0, 4.0), (3.0, 4.0))],
    )
    assert distance == pytest.approx(5.0)

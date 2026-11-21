from rowx_sqlite._core import type_table
from rowx_sqlite._types import accepts, by_key, result_type


def test_the_table_has_one_row_per_kind() -> None:
    assert [info.key for info in type_table()] == [
        "integer",
        "real",
        "text",
        "blob",
        "boolean",
        "datetime",
        "datetimetz",
        "date",
        "time",
        "decimal",
        "uuid",
        "json",
        "any",
    ]


def test_lookup_by_key() -> None:
    assert by_key("integer").py_result == "int"
    assert result_type("decimal") == "decimal.Decimal"
    assert result_type("any") == "int | float | str | bytes"


def test_naive_and_aware_datetimes_differ_by_zone_rule() -> None:
    assert by_key("datetime").tz == "naive"
    assert by_key("datetimetz").tz == "aware"
    assert by_key("integer").tz == "none"


def test_accepted_parameters_are_the_python_types_that_bind_to_a_kind() -> None:
    assert accepts("int", "integer")
    assert not accepts("str", "integer")
    assert accepts("memoryview", "blob")
    assert accepts("dict", "json")
    assert accepts("UUID", "uuid")


def test_every_row_names_a_codec_family() -> None:
    assert {info.codec for info in type_table()} == {
        "integer",
        "real",
        "text",
        "blob",
        "boolean",
        "temporal",
        "decimal",
        "uuid",
        "json",
        "any",
    }

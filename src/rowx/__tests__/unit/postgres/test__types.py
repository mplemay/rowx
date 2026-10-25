from typing import TypeAliasType, get_args

import pytest

from rowx._core.postgres import type_table
from rowx.postgres._types import accepts, by_key, result_type
from rowx.postgres._vocab import AcceptedParam, CodecName, PyResultName, TypeKey


def test_type_table_has_core_keys() -> None:
    keys = {t.key for t in type_table()}
    assert keys >= {"int4", "numeric", "timestamptz", "uuid", "jsonb", "text", "char", "void"}


def test_by_key_and_result_type() -> None:
    assert by_key("int4").matcher_oid == 23
    assert result_type("timestamptz") == "datetime.datetime"
    assert result_type("numeric") == "decimal.Decimal"


def test_by_key_unknown_raises() -> None:
    with pytest.raises(KeyError):
        by_key("nope")


def test_accepts() -> None:
    assert accepts("str", "text")
    assert not accepts("bytes", "text")
    assert accepts("int", "int4")
    assert not accepts("bool", "int4")
    assert accepts("UUID", "uuid")
    assert accepts("str", "uuid")


@pytest.mark.parametrize("key", ["json", "jsonb"])
@pytest.mark.parametrize("py_type_name", ["str", "int", "bool", "float", "list", "dict", "None"])
def test_json_accepts_any_value(py_type_name: str, key: str) -> None:
    assert accepts(py_type_name, key)


def test_text_accepts_only_str() -> None:
    assert by_key("text").accepted_params == ["str"]


def _names(alias: TypeAliasType) -> set[str]:
    return set(get_args(alias.__value__))


def test_vocab_matches_the_type_table() -> None:
    rows = type_table()
    assert {row.key for row in rows} == _names(TypeKey)
    assert {row.py_result for row in rows} == _names(PyResultName)
    assert {name for row in rows for name in row.accepted_params} == _names(AcceptedParam)
    assert {codec for row in rows for codec in (row.decode, row.encode)} == _names(CodecName)


def test_type_info_is_frozen() -> None:
    with pytest.raises(AttributeError):
        by_key("text").key = "x"  # type: ignore[misc]  # ty: ignore[invalid-assignment]

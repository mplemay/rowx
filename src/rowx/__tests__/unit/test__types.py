import pytest

from rowx import _core
from rowx._types import accepts, by_key, result_type


def test_type_table_has_core_keys() -> None:
    keys = {t.key for t in _core.type_table()}
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


def test_text_accepts_only_str() -> None:
    assert by_key("text").accepted_params == ["str"]


def test_type_info_is_frozen() -> None:
    with pytest.raises(AttributeError):
        by_key("text").key = "x"  # type: ignore[misc]  # ty: ignore[invalid-assignment]

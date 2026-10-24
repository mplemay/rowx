from functools import cache
from typing import TYPE_CHECKING

from rowx._core import TypeInfo, type_table

if TYPE_CHECKING:
    from rowx._vocab import PyResultName


@cache
def _by_key() -> dict[str, TypeInfo]:
    return {t.key: t for t in type_table()}


def by_key(key: str) -> TypeInfo:
    return _by_key()[key]


def accepts(py_type_name: str, key: str) -> bool:
    accepted = by_key(key).accepted_params
    # `object` (json/jsonb) takes any value; json.dumps is the arbiter at runtime
    return "object" in accepted or py_type_name in accepted


def result_type(key: str) -> PyResultName:
    return by_key(key).py_result

from functools import cache
from typing import TYPE_CHECKING

from rowx._core.postgres import TypeInfo, type_table

if TYPE_CHECKING:
    from rowx.postgres._vocab import PyResultName


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


def result_types(key: str) -> frozenset[str]:
    # the leaves a column may be annotated with: the default and any alternative the annotation can pick
    info = by_key(key)
    return frozenset({info.py_result, *info.alt_py_results})

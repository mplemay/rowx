from functools import cache

from rowx._core import TypeInfo, type_table


@cache
def _by_key() -> dict[str, TypeInfo]:
    return {t.key: t for t in type_table()}


def by_key(key: str) -> TypeInfo:
    return _by_key()[key]


def accepts(py_type_name: str, key: str) -> bool:
    return py_type_name in by_key(key).accepted_params


def result_type(key: str) -> str:
    return by_key(key).py_result

from functools import cache
from typing import TYPE_CHECKING

from rowx_sqlite._core import type_table

if TYPE_CHECKING:
    from rowx_sqlite._core import TypeInfo, TypeKey


@cache
def _by_key() -> dict[str, TypeInfo]:
    return {t.key: t for t in type_table()}


def by_key(key: TypeKey) -> TypeInfo:
    return _by_key()[key]


def accepts(py_type_name: str, key: TypeKey) -> bool:
    return py_type_name in by_key(key).accepted_params


def result_type(key: TypeKey) -> str:
    return by_key(key).py_result

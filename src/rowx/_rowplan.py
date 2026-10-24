import datetime as dt
import decimal
import types
import uuid
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal, TypeAliasType, Union, get_args, get_origin

from rowx.errors import InterfaceError

MAX_ALIAS_DEPTH: Final[int] = 16

LEAVES: Final[frozenset[type]] = frozenset(
    {
        int,
        float,
        str,
        bool,
        bytes,
        decimal.Decimal,
        dt.date,
        dt.time,
        dt.datetime,
        dt.timedelta,
        uuid.UUID,
    },
)

type PlanKind = Literal["none", "scalar", "tuple"]


@dataclass(frozen=True, slots=True, kw_only=True)
class RowPlan:
    kind: PlanKind
    arity: int
    nullable_mask: tuple[bool, ...]


NO_ROWS: Final[RowPlan] = RowPlan(kind="none", arity=0, nullable_mask=())

_cache: dict[object, RowPlan] = {}


def _reject(t: object, why: str) -> InterfaceError:
    return InterfaceError(f"unsupported row type {t!r}: {why}")


def _unwrap(t: object) -> object:
    for _ in range(MAX_ALIAS_DEPTH + 1):
        if get_origin(t) is Annotated:
            t = get_args(t)[0]
        elif isinstance(t, TypeAliasType):
            if t.__type_params__:
                raise _reject(t, "generic aliases are not supported")
            t = t.__value__
        else:
            return t
    msg = f"alias chain deeper than {MAX_ALIAS_DEPTH}"
    raise _reject(t, msg)


def _is_none(t: object) -> bool:
    return t is None or t is type(None)


def _elem(t: object) -> bool:
    t = _unwrap(t)
    if _is_none(t) or t is Any or t is object:
        return True
    origin = get_origin(t)
    if origin is Union or origin is types.UnionType:
        members = [m for m in get_args(t) if not _is_none(_unwrap(m))]
        if len(members) != 1:
            raise _reject(t, "a union must have exactly one non-None member")
        _elem(members[0])
        return True
    if origin is list:
        (inner,) = get_args(t)
        _elem(inner)
        return False
    if origin is None and isinstance(t, type) and t in LEAVES:
        return False
    raise _reject(t, "not derivable from the row type grammar")


def _build(t: object) -> RowPlan:
    t = _unwrap(t)
    if _is_none(t):
        return NO_ROWS
    if get_origin(t) is tuple:
        args = get_args(t)
        if not args or Ellipsis in args:
            raise _reject(t, "a tuple needs fixed arity of at least 1")
        mask = tuple(_elem(a) for a in args)
        return RowPlan(kind="tuple", arity=len(mask), nullable_mask=mask)
    return RowPlan(kind="scalar", arity=1, nullable_mask=(_elem(t),))


def row_plan(t: object) -> RowPlan:
    try:
        return _cache[t]
    except KeyError:
        pass
    except TypeError:
        # unhashable annotation metadata: build without caching
        return _build(t)
    return _cache.setdefault(t, _build(t))

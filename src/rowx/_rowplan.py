import datetime as dt
import decimal
import ipaddress
import sys
import types
import uuid
from dataclasses import dataclass, field
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

# each pair is one leaf (inet and cidr); neither member is a leaf on its own
NETWORK_PAIRS: Final[frozenset[frozenset[type]]] = frozenset(
    {
        frozenset({ipaddress.IPv4Interface, ipaddress.IPv6Interface}),
        frozenset({ipaddress.IPv4Network, ipaddress.IPv6Network}),
    },
)

# the subtype leaves a range column can have
RANGE_SUBTYPES: Final[frozenset[type]] = frozenset({int, decimal.Decimal, dt.date, dt.datetime})

type PlanKind = Literal["none", "scalar", "tuple"]


@dataclass(frozen=True, slots=True, kw_only=True)
class RowPlan:
    kind: PlanKind
    arity: int
    nullable_mask: tuple[bool, ...]
    # per element, whether it is `rowx.postgres.Interval`, which reads an `interval` losslessly
    # (left out of equality so a plan compares by its shape and nullability)
    interval_mask: tuple[bool, ...] = field(default=(), compare=False)

    @property
    def flags(self) -> list[int]:
        # one int per column for the driver: bit 0 is nullable, bit 1 is the `Interval` leaf
        spans = self.interval_mask or (False,) * len(self.nullable_mask)
        return [int(null) | (int(span) << 1) for null, span in zip(self.nullable_mask, spans, strict=True)]


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


def _flatten(t: object, depth: int = 0) -> tuple[list[object], bool]:
    # the members of a union with nested unions (through aliases or `Annotated`) spliced in, and whether `None` is one
    if depth > MAX_ALIAS_DEPTH:
        raise _reject(t, f"alias chain deeper than {MAX_ALIAS_DEPTH}")
    t = _unwrap(t)
    if _is_none(t):
        return [], True
    if get_origin(t) not in {Union, types.UnionType}:
        return [t], False
    members: list[object] = []
    has_none = False
    for arg in get_args(t):
        inner, inner_none = _flatten(arg, depth + 1)
        members.extend(inner)
        has_none = has_none or inner_none
    return members, has_none


# `rowx.postgres.Range` is looked up only when it was imported, because importing it loads the backend, and a
# row type can only mention it after the user's own import
def _is_range(origin: object) -> bool:
    module = sys.modules.get("rowx.postgres._range")
    return module is not None and origin is module.Range


def _is_value_class(t: object, module_name: str, name: str) -> bool:
    module = sys.modules.get(module_name)
    return module is not None and t is getattr(module, name)


def _is_interval(t: object) -> bool:
    return _is_value_class(t, "rowx.postgres._interval", "Interval")


def _is_money(t: object) -> bool:
    return _is_value_class(t, "rowx.postgres._money", "Money")


def _elem(t: object) -> bool:
    t = _unwrap(t)
    if _is_none(t) or t is Any or t is object:
        return True
    origin = get_origin(t)
    if origin is Union or origin is types.UnionType:
        members, has_none = _flatten(t)
        if len(members) > 1 and all(isinstance(m, type) for m in members) and frozenset(members) in NETWORK_PAIRS:
            return has_none
        if len(members) != 1:
            raise _reject(t, "a union must have exactly one non-None member")
        _elem(members[0])
        return True
    if origin is list:
        (inner,) = get_args(t)
        _elem(inner)
        return False
    if _is_range(origin):
        (subtype,) = get_args(t)
        if _unwrap(subtype) not in RANGE_SUBTYPES:
            raise _reject(t, "a range subtype must be int, Decimal, date or datetime")
        return False
    if origin is None and isinstance(t, type) and (t in LEAVES or _is_interval(t) or _is_money(t)):
        return False
    raise _reject(t, "not derivable from the row type grammar")


def _is_interval_elem(t: object) -> bool:
    # `Interval` and `Interval | None`, also inside lists, since the codec choice covers array elements
    t = _unwrap(t)
    if get_origin(t) is list:
        return _is_interval_elem(get_args(t)[0])
    members, _ = _flatten(t)
    return len(members) == 1 and _is_interval(members[0])


def _build(t: object) -> RowPlan:
    t = _unwrap(t)
    if _is_none(t):
        return NO_ROWS
    if get_origin(t) is tuple:
        args = get_args(t)
        if not args or Ellipsis in args:
            raise _reject(t, "a tuple needs fixed arity of at least 1")
        mask = tuple(_elem(a) for a in args)
        spans = tuple(_is_interval_elem(a) for a in args)
        return RowPlan(kind="tuple", arity=len(mask), nullable_mask=mask, interval_mask=spans)
    return RowPlan(
        kind="scalar",
        arity=1,
        nullable_mask=(_elem(t),),
        interval_mask=(_is_interval_elem(t),),
    )


def row_plan(t: object) -> RowPlan:
    try:
        return _cache[t]
    except KeyError:
        pass
    except TypeError:
        # unhashable annotation metadata: build without caching
        return _build(t)
    return _cache.setdefault(t, _build(t))

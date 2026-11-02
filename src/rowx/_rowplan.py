import datetime as dt
import decimal
import ipaddress
import sys
import types
import uuid
from annotationlib import Format, get_annotations
from dataclasses import MISSING, dataclass, field, is_dataclass
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Final,
    Literal,
    NotRequired,
    ReadOnly,
    Required,
    TypeAliasType,
    Union,
    get_args,
    get_origin,
    get_type_hints,
    is_protocol,
    is_typeddict,
)

from rowx.errors import InterfaceError

if TYPE_CHECKING:
    from collections.abc import Callable

    from rowx._core.postgres import RowShape

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

type PlanKind = Literal["none", "scalar", "tuple", "named"]
type RowForm = Literal["positional", "keyword", "dict"]
type Matching = Literal["by_name", "positional"]

# the wrappers a named type's field annotation may carry around its element
FIELD_QUALIFIERS: Final[tuple[object, ...]] = (Required, NotRequired, ReadOnly)

# `dataclasses.Field._field_type` names; the sentinels are private to `dataclasses`
DATACLASS_FIELD: Final[str] = "_FIELD"
DATACLASS_INITVAR: Final[str] = "_FIELD_INITVAR"


@dataclass(frozen=True, slots=True, kw_only=True)
class FieldPlan:
    key: str
    # the column names the field accepts; `None` is unknown (not used until a form can have aliases)
    names: tuple[str, ...] | None
    nullable: bool
    interval: bool
    required: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class RowPlan:
    kind: PlanKind
    arity: int
    nullable_mask: tuple[bool, ...]
    # per element, whether it is `rowx.postgres.Interval`, which reads an `interval` losslessly
    # (left out of equality so a plan compares by its shape and nullability)
    interval_mask: tuple[bool, ...] = field(default=(), compare=False)
    # a named plan only
    fields: tuple[FieldPlan, ...] = ()
    form: RowForm | None = None
    matching: Matching | None = None
    type_name: str | None = None
    # the `_core.postgres.RowShape` the driver maps columns and builds rows with, built once with the plan
    shape: RowShape | None = field(default=None, compare=False)

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


GEOMETRIC_CLASSES: Final[tuple[str, ...]] = ("Point", "Line", "LineSegment", "Box", "Path", "Polygon", "Circle")


def _is_geometric(t: object) -> bool:
    return any(_is_value_class(t, "rowx.postgres._geometric", name) for name in GEOMETRIC_CLASSES)


def _is_cube(t: object) -> bool:
    return _is_value_class(t, "rowx.postgres._cube", "Cube")


def _is_value_leaf(t: type) -> bool:
    return t in LEAVES or _is_interval(t) or _is_money(t) or _is_geometric(t) or _is_cube(t)


# the one dict leaf: an hstore, `dict[str, str | None]`
def _is_hstore(t: object) -> bool:
    key, value = get_args(t)
    members, has_none = _flatten(value)
    return _unwrap(key) is str and members == [str] and has_none


def _union_elem(t: object) -> bool:
    members, has_none = _flatten(t)
    if len(members) > 1 and all(isinstance(m, type) for m in members) and frozenset(members) in NETWORK_PAIRS:
        return has_none
    if len(members) != 1:
        raise _reject(t, "a union must have exactly one non-None member")
    _elem(members[0])
    return True


def _elem(t: object) -> bool:
    t = _unwrap(t)
    if _is_none(t) or t is Any or t is object:
        return True
    origin = get_origin(t)
    if origin is Union or origin is types.UnionType:
        return _union_elem(t)
    if origin is list:
        (inner,) = get_args(t)
        _elem(inner)
        return False
    if origin is dict and not _is_hstore(t):
        raise _reject(t, "a dict must be `dict[str, str | None]`, the hstore leaf")
    if origin is dict:
        return False
    if _is_range(origin):
        (subtype,) = get_args(t)
        if _unwrap(subtype) not in RANGE_SUBTYPES:
            raise _reject(t, "a range subtype must be int, Decimal, date or datetime")
        return False
    if origin is None and isinstance(t, type) and _is_value_leaf(t):
        return False
    if isinstance(t, type) and _named_form(t) is not None:
        raise _reject(t, "a named row type is only allowed as the row, not inside a tuple, list, union or field")
    raise _reject(t, "not derivable from the row type grammar")


def _is_interval_elem(t: object) -> bool:
    # `Interval` and `Interval | None`, also inside lists, since the codec choice covers array elements
    members, _ = _flatten(_unwrap(t))
    if len(members) != 1:
        return False
    # flatten first so `list[Interval] | None` is seen as the list it wraps
    (member,) = members
    if get_origin(member) is list:
        return _is_interval_elem(get_args(member)[0])
    return _is_interval(member)


# `typing_extensions.TypedDict` is only looked up when it was imported, as `typing.is_typeddict` misses it
def _is_typeddict(t: object) -> bool:
    if is_typeddict(t):
        return True
    module = sys.modules.get("typing_extensions")
    return module is not None and bool(module.is_typeddict(t))


def _is_pydantic(t: type) -> bool:
    return sys.modules.get("pydantic") is not None and (
        hasattr(t, "__pydantic_validator__") or hasattr(t, "__pydantic_fields__")
    )


def _is_namedtuple(t: type) -> bool:
    return issubclass(t, tuple) and isinstance(getattr(t, "_fields", None), tuple)


def _is_protocol(t: type) -> bool:
    return is_protocol(t)


# the checks for each named form, in order; a Protocol and a pydantic class are named only to be rejected
NAMED_FORMS: Final[tuple[tuple[str, Callable[[type], bool]], ...]] = (
    ("protocol", _is_protocol),
    ("pydantic", _is_pydantic),
    ("typeddict", _is_typeddict),
    ("namedtuple", _is_namedtuple),
    ("dataclass", is_dataclass),
)


# which named form a class is, or `None` for any other class; the value leaves are frozen dataclasses and
# are never named, so this is only asked of a class that is not a leaf
def _named_form(t: type) -> str | None:
    if _is_value_leaf(t) or _is_range(t):
        return None
    return next((name for name, matches in NAMED_FORMS if matches(t)), None)


def _unwrap_field(t: object) -> object:
    for _ in range(MAX_ALIAS_DEPTH + 1):
        origin = get_origin(t)
        if origin is Annotated or origin in FIELD_QUALIFIERS:
            t = get_args(t)[0]
        elif isinstance(t, TypeAliasType) and not t.__type_params__:
            t = t.__value__
        else:
            return t
    return t


def _missing_name_field(t: type, name: str | None) -> str | None:
    # the annotations unresolved names stay as `ForwardRef`s, which says which field mentions the name
    for klass in reversed(t.__mro__):
        for key, annotation in get_annotations(klass, format=Format.FORWARDREF).items():
            if name is not None and name in repr(annotation) and "ForwardRef" in repr(annotation):
                return key
    return None


def _hints(t: type) -> dict[str, object]:
    try:
        return dict(get_type_hints(t, include_extras=True))
    except NameError as err:
        key = _missing_name_field(t, err.name)
        where = f"field {key!r}" if key is not None else "an annotation"
        msg = f"{t.__name__}: {where} names {err.name!r}, which is not available at runtime; import it outside `TYPE_CHECKING`"
        raise InterfaceError(msg) from err


@dataclass(frozen=True, slots=True, kw_only=True)
class Spec:
    key: str
    required: bool


def _dataclass_specs(t: type) -> list[Spec]:
    if not t.__dataclass_params__.init:  # ty: ignore[unresolved-attribute]
        raise _reject(t, "a dataclass with init=False is not supported")
    specs: list[Spec] = []
    for key, f in t.__dataclass_fields__.items():  # ty: ignore[unresolved-attribute]
        kind = f._field_type.name  # ruff: ignore[private-member-access]
        if kind == DATACLASS_INITVAR:
            raise _reject(t, f"InitVar field {key!r} is not supported")
        if kind == DATACLASS_FIELD and f.init:
            specs.append(Spec(key=key, required=f.default is MISSING and f.default_factory is MISSING))
    return specs


def _namedtuple_specs(t: type) -> list[Spec]:
    defaults = getattr(t, "_field_defaults", {})
    return [Spec(key=key, required=key not in defaults) for key in t._fields]  # ty: ignore[unresolved-attribute]


def _typeddict_specs(t: type, hints: dict[str, object]) -> list[Spec]:
    required = t.__required_keys__  # ty: ignore[unresolved-attribute]
    return [Spec(key=key, required=key in required) for key in hints]


def _build_shape(t: type, form: RowForm, matching: Matching, fields: tuple[FieldPlan, ...]) -> RowShape:
    # `rowx._core` is the backend, which `import rowx` must not load, so it is imported when a plan needs it
    from rowx._core.postgres import RowShape as RowShapeImpl  # ruff: ignore[import-outside-top-level]

    flags = [(int(f.nullable) | (int(f.interval) << 1)) for f in fields]
    return RowShapeImpl(
        t,
        form,
        matching,
        t.__name__,
        [(f.key, f.names, f.required, flag) for f, flag in zip(fields, flags, strict=True)],
    )


def _named(t: type, kind: str) -> RowPlan:
    match kind:
        case "protocol":
            raise _reject(t, "a Protocol is not a row type")
        case "pydantic":
            raise _reject(t, "pydantic models are not supported yet")
    hints = _hints(t)
    form: RowForm
    matching: Matching
    match kind:
        case "dataclass":
            specs, form, matching = _dataclass_specs(t), "keyword", "by_name"
        case "namedtuple":
            specs, form, matching = _namedtuple_specs(t), "positional", "positional"
        case _:
            specs, form, matching = _typeddict_specs(t, hints), "dict", "by_name"
    if not specs:
        raise _reject(t, "has no fields")
    plans: list[FieldPlan] = []
    for spec in specs:
        if spec.key not in hints:
            raise _reject(
                t, f"field {spec.key!r} has no type annotation; use typing.NamedTuple, not collections.namedtuple"
            )
        annotation = _unwrap_field(hints[spec.key])
        try:
            nullable = _elem(annotation)
            interval = _is_interval_elem(annotation)
        except InterfaceError as err:
            msg = f"{err} (field {spec.key!r} of {t.__name__})"
            raise InterfaceError(msg) from err
        plans.append(
            FieldPlan(key=spec.key, names=(spec.key,), nullable=nullable, interval=interval, required=spec.required),
        )
    fields = tuple(plans)
    return RowPlan(
        kind="named",
        arity=len(fields),
        nullable_mask=tuple(f.nullable for f in fields),
        interval_mask=tuple(f.interval for f in fields),
        fields=fields,
        form=form,
        matching=matching,
        type_name=t.__name__,
        shape=_build_shape(t, form, matching, fields),
    )


def _nullable_named(t: object) -> bool:
    if get_origin(t) not in {Union, types.UnionType}:
        return False
    members, has_none = _flatten(t)
    return has_none and len(members) == 1 and isinstance(members[0], type) and _named_form(members[0]) is not None


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
    if isinstance(t, type) and (kind := _named_form(t)) is not None:
        return _named(t, kind)
    if _nullable_named(t):
        raise _reject(t, "a named row cannot be nullable; use .optional()")
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

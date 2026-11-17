import datetime as dt
import decimal
import ipaddress
import re
import sys
import types
import uuid
from annotationlib import Format, get_annotations
from dataclasses import MISSING, InitVar, dataclass, field, fields, is_dataclass
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    Final,
    ForwardRef,
    Generic,
    Literal,
    NotRequired,
    ParamSpec,
    Protocol,
    ReadOnly,
    Required,
    TypeAliasType,
    TypeVar,
    TypeVarTuple,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
    is_protocol,
    is_typeddict,
)

from rowx_core.errors import InterfaceError

if TYPE_CHECKING:
    from collections.abc import Callable

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
type RowForm = Literal["positional", "keyword", "dict", "validator"]
type Matching = Literal["by_name", "positional"]

# the wrappers a named type's field annotation may carry around its element
FIELD_QUALIFIERS: Final[dict[str, object]] = {"Required": Required, "NotRequired": NotRequired, "ReadOnly": ReadOnly}

# the text of a `ClassVar` annotation that stayed a string (`from __future__ import annotations`)
CLASSVAR_TEXT: Final[re.Pattern[str]] = re.compile(r"^\s*(?:\w+\.)*ClassVar\b")


class ValueClass:
    # empty so a slotted dataclass subclass does not grow a __dict__
    __slots__ = ()
    _rowx_flags: ClassVar[int] = 0
    _rowx_subscripted: ClassVar[bool] = False


@dataclass(frozen=True, slots=True, kw_only=True)
class FieldPlan:
    key: str
    # the column names the field accepts; `None` is unknown (a pydantic `AliasChoices` or a before validator)
    names: tuple[str, ...] | None
    nullable: bool
    # `flags` is the value-class mask; bit 0 stays clear, and the driver mask is `int(nullable) | flags`
    flags: int
    required: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class ShapeSpec:
    ctor: object
    form: RowForm
    matching: Matching
    type_name: str
    fields: tuple[FieldPlan, ...]
    validate: bool = True
    # native shapes, keyed by the backend's row-shape class; a second key does not evict the first
    native: dict[object, object] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class RowPlan:
    kind: PlanKind
    arity: int
    nullable_mask: tuple[bool, ...]
    # registered value-class flags; excluded from equality, same as the shape
    class_flags: tuple[int, ...] = field(default=(), compare=False)
    # a named plan only
    fields: tuple[FieldPlan, ...] = ()
    form: RowForm | None = None
    matching: Matching | None = None
    type_name: str | None = None
    # pure-Python shape; the backend builds and caches the native shape from this
    shape: ShapeSpec | None = field(default=None, compare=False)

    @property
    def flags(self) -> list[int]:
        # one int per column for the driver: bit 0 is nullable, the rest come from the value class
        spans = self.class_flags or (0,) * len(self.nullable_mask)
        return [int(null) | span for null, span in zip(self.nullable_mask, spans, strict=True)]


NO_ROWS: Final[RowPlan] = RowPlan(kind="none", arity=0, nullable_mask=())

_cache: dict[object, RowPlan] = {}


def _reject(t: object, why: str) -> InterfaceError:
    return InterfaceError(f"unsupported row type {t!r}: {why}")


class _NameUnavailableError(InterfaceError):
    def __init__(self, t: object, name: str | None) -> None:
        super().__init__(
            f"unsupported row type {t!r}: it names {name!r}, which is not available at runtime; "
            "import it outside `TYPE_CHECKING`"
        )
        self.name = name


# a `type` statement evaluates its value lazily, so a name imported only under `TYPE_CHECKING` fails here
def _alias_value(t: TypeAliasType) -> object:
    try:
        return t.__value__
    except NameError as err:
        raise _NameUnavailableError(t, err.name) from err


# the text a type argument is shown as in a `type_name`, such as the `int | None` of `Box[int | None]`
def _type_text(t: object) -> str:
    if t is None or t is type(None) or t is Ellipsis:
        return "..." if t is Ellipsis else "None"
    if isinstance(t, type | TypeAliasType | TypeVar):
        return t.__name__
    origin, args = get_origin(t), get_args(t)
    if origin is Annotated:
        return _type_text(args[0])
    if origin in {Union, types.UnionType}:
        return " | ".join(_type_text(arg) for arg in args)
    if isinstance(origin, type | TypeAliasType) and args:
        return f"{_type_text(origin)}[{', '.join(_type_text(arg) for arg in args)}]"
    return repr(t)


# a substitution is the type's own: `t[args]` fills each `__parameters__` entry, whatever order the type uses them in
def _subst(t: object, mapping: dict[object, object]) -> object:
    if not mapping or isinstance(t, type | TypeAliasType):
        return t
    if isinstance(t, TypeVar):
        return mapping.get(t, t)
    if not (params := getattr(t, "__parameters__", None)):
        return t
    try:
        return t[tuple(mapping.get(p, p) for p in params)]  # ty: ignore[not-subscriptable]
    except TypeError as err:
        raise _reject(t, f"it can't take {mapping!r}: {err}") from err


# each parameter bound to its argument, or else to its PEP 696 default; either may name the parameters before it,
# because Python fills a default in unsubstituted (`C[int].__args__ == (int, list[A])`)
def _bind(owner: object, name: str, params: tuple[object, ...], args: tuple[object, ...]) -> dict[object, object]:
    typevars: list[TypeVar] = []
    for param in params:
        if isinstance(param, TypeVar):
            typevars.append(param)
            continue
        kind = "TypeVarTuple" if isinstance(param, TypeVarTuple) else "ParamSpec"
        raise _reject(owner, f"`{name}` has the {kind} `{getattr(param, '__name__', param)}`, which is not supported")
    required = [param for param in typevars if not param.has_default()]
    if not args and required:
        msg = f"the type parameter `{required[0].__name__}` of `{name}` is not bound; subscript it, for example `{name}[int]`"
        raise _reject(owner, msg)
    if not len(required) <= len(args) <= len(typevars):
        takes = str(len(typevars)) if len(required) == len(typevars) else f"{len(required)} to {len(typevars)}"
        raise _reject(owner, f"`{name}` takes {takes} type arguments, but {len(args)} were given")
    mapping: dict[object, object] = {}
    for index, param in enumerate(typevars):
        mapping[param] = _subst(args[index] if index < len(args) else param.__default__, mapping)
    return mapping


def _expand(alias: TypeAliasType, t: object, args: tuple[object, ...]) -> object:
    mapping = _bind(t, alias.__name__, alias.__type_params__, args)
    return _subst(_alias_value(alias), mapping)


# `t` with its aliases and `Annotated` layers removed, and how many hops that took; a generic alias is substituted
# through its own parameters, never `__value__[args]`
def _unwrap_hops(t: object) -> tuple[object, int]:
    for hops in range(MAX_ALIAS_DEPTH + 1):
        origin = get_origin(t)
        if origin is Annotated:
            t = get_args(t)[0]
        elif isinstance(t, TypeAliasType):
            t = _expand(t, t, ())
        elif isinstance(origin, TypeAliasType):
            t = _expand(origin, t, get_args(t))
        else:
            return t, hops
    msg = f"alias chain deeper than {MAX_ALIAS_DEPTH}"
    raise _reject(t, msg)


def _unwrap(t: object) -> object:
    return _unwrap_hops(t)[0]


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


def _value_marker(cls: type[ValueClass]) -> tuple[int, bool]:
    return (cls._rowx_flags, cls._rowx_subscripted)


def _value_leaves(t: type) -> list[type]:
    return [base for base in t.__mro__ if base is not ValueClass and ValueClass in base.__bases__]


def _direct_leaf(t: type) -> type[ValueClass] | None:
    return cast("type[ValueClass]", t) if _value_leaves(t) == [t] else None


def _public_name(cls: type) -> str:
    parts = cls.__module__.split(".")
    while len(parts) > 1 and parts[-1].startswith("_"):
        parts.pop()
    return f"{'.'.join(parts)}.{cls.__qualname__}"


def _marker_flags(t: object, leaf: type[ValueClass]) -> int:
    flags, _ = _value_marker(leaf)
    if flags & 1:
        raise _reject(t, "a value class flag uses bit 0, which is reserved for nullability")
    return flags


def _is_subscripted_value(origin: object) -> bool:
    if not isinstance(origin, type) or (leaf := _direct_leaf(origin)) is None:
        return False
    _, subscripted = _value_marker(leaf)
    return subscripted


def _is_value_leaf(t: type) -> bool:
    if t in LEAVES:
        return True
    if (leaf := _direct_leaf(t)) is None:
        return False
    _, subscripted = _value_marker(leaf)
    return not subscripted


def _leaf_flags(t: object) -> int:
    if isinstance(t, type):
        leaf = _direct_leaf(t)
        if leaf is None or _value_marker(leaf)[1]:
            return 0
        return _marker_flags(t, leaf)
    origin = get_origin(t)
    if isinstance(origin, type) and (leaf := _direct_leaf(origin)) is not None and _value_marker(leaf)[1]:
        return _marker_flags(t, leaf)
    return 0


def _value_flags(t: object) -> int:
    # flags of the one leaf, also inside lists, since the codec choice covers array elements
    members, _ = _flatten(_unwrap(t))
    if len(members) != 1:
        return 0
    (member,) = members
    if get_origin(member) is list:
        return _value_flags(get_args(member)[0])
    return _leaf_flags(member)


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


def _not_derivable(t: object) -> InterfaceError:
    if isinstance(t, TypeVar | TypeVarTuple | ParamSpec):
        msg = f"the type parameter `{t.__name__}` is not bound; subscript the generic class or alias, for example `Box[int]`"
        return _reject(t, msg)
    return _reject(t, "not derivable from the row type grammar")


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
    if _is_subscripted_value(origin):
        (subtype,) = get_args(t)
        if _unwrap(subtype) not in RANGE_SUBTYPES:
            raise _reject(t, "a range subtype must be int, Decimal, date or datetime")
        return False
    if origin is None and isinstance(t, type) and _is_value_leaf(t):
        return False
    if isinstance(t, type) and _named_form(t) is not None:
        raise _reject(t, "a named row type is only allowed as the row, not inside a tuple, list, union or field")
    raise _not_derivable(t)


# `typing_extensions.TypedDict` is only looked up when it was imported, as `typing.is_typeddict` misses it
def _is_typeddict(t: object) -> bool:
    if is_typeddict(t):
        return True
    module = sys.modules.get("typing_extensions")
    return module is not None and bool(module.is_typeddict(t))


# a pydantic class is looked up only when pydantic was imported, and rowx never imports it
def _pydantic_dataclass(t: type) -> bool:
    module = sys.modules.get("pydantic.dataclasses")
    return module is not None and bool(module.is_pydantic_dataclass(t))


def _is_pydantic(t: type) -> bool:
    pydantic = sys.modules.get("pydantic")
    if pydantic is None:
        return False
    v1 = sys.modules.get("pydantic.v1")
    return (
        issubclass(t, pydantic.BaseModel) or (v1 is not None and issubclass(t, v1.BaseModel)) or _pydantic_dataclass(t)
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
    if _is_value_leaf(t) or _is_subscripted_value(t):
        return None
    if leaves := _value_leaves(t):
        owner = next(leaf for leaf in leaves if leaf is not t)
        raise _reject(t, f"a subclass of {_public_name(owner)} is not a row type")
    return next((name for name, matches in NAMED_FORMS if matches(t)), None)


def _qualifier(origin: object) -> str | None:
    # `typing` and `typing_extensions` each own their qualifiers on some versions
    if origin is None:
        return None
    extensions = sys.modules.get("typing_extensions")
    for name, qualifier in FIELD_QUALIFIERS.items():
        if origin is qualifier or (extensions is not None and origin is getattr(extensions, name, None)):
            return name
    return None


def _unwrap_field(t: object) -> object:
    for _ in range(MAX_ALIAS_DEPTH + 1):
        origin = get_origin(t)
        if origin is Annotated or _qualifier(origin) is not None:
            t = get_args(t)[0]
        elif isinstance(t, TypeAliasType) and not t.__type_params__:
            t = _alias_value(t)
        else:
            return t
    return t


# the declaring class and raw (possibly unresolved) annotation of each field, most derived class first
def _declared(t: type) -> dict[str, tuple[type, object]]:
    if _is_typeddict(t):
        return _declared_typeddict(t)
    out: dict[str, tuple[type, object]] = {}
    for klass in t.__mro__:
        for key, annotation in get_annotations(klass, format=Format.FORWARDREF).items():
            out.setdefault(key, (klass, annotation))
    return out


# a TypedDict is not a subclass of its bases and merges their annotations, so a key's declaring class is the base
# whose annotation it still has, or else the class itself
def _declared_typeddict(t: type) -> dict[str, tuple[type, object]]:
    inherited: dict[str, tuple[type, object]] = {}
    for base in getattr(t, "__orig_bases__", ()):
        # a generic base is subscripted (`Base[U]`), and its keys are declared by the class it subscripts
        origin = get_origin(base) or base
        if isinstance(origin, type) and _is_typeddict(origin):
            for key, owned in _declared_typeddict(origin).items():
                inherited.setdefault(key, owned)
    out: dict[str, tuple[type, object]] = {}
    for key, annotation in get_annotations(t, format=Format.FORWARDREF).items():
        owned = inherited.get(key)
        # attributing an equal redeclared key to the base is safe: TypedDict `ForwardRef`s carry their module, so a
        # same-text redeclaration from another module compares unequal
        out[key] = owned if owned is not None and owned[1] == annotation else (t, annotation)
    return out


# one annotation resolved in the namespace of the class that declares it; a name that is not available raises `NameError`
def _resolve(klass: type, annotation: object, key: str) -> object:
    holder = type("Holder", (), {"__annotations__": {key: annotation}})
    module = sys.modules.get(klass.__module__)
    # a quoted `T` names a type parameter, which lives outside the class body
    params = {param.__name__: param for param in getattr(klass, "__parameters__", ())}
    return get_type_hints(
        holder,
        globalns=vars(module) if module is not None else {},
        localns={**params, **vars(klass)},
        include_extras=True,
    )[key]


type Bindings = dict[type, dict[object, object]]


# the arguments of each generic class along the bases of `root`, which is bound by `mapping`: `Child[U](Base[U])`
# gives `Base`'s own `T` the argument `U` has, because `get_type_hints` still reports `T` for an inherited field
def _class_bindings(root: type, mapping: dict[object, object]) -> Bindings:
    out: Bindings = {root: mapping}
    pending = [(root, 0)]
    while pending:
        klass, depth = pending.pop(0)
        for base in klass.__dict__.get("__orig_bases__", klass.__bases__):
            origin = get_origin(base) or base
            # `Generic[T]` and `Protocol[T]` add no bindings and are not classes the checker follows
            if origin is Generic or origin is Protocol:
                continue
            if not isinstance(origin, type) or origin in out:
                continue
            # only a subscripted base is a hop; a plain base adds no depth
            subscripted = get_origin(base) is not None
            if subscripted and depth >= MAX_ALIAS_DEPTH:
                raise _reject(root, f"generic bases deeper than {MAX_ALIAS_DEPTH}")
            params = getattr(origin, "__parameters__", ())
            args = tuple(_subst(arg, out[klass]) for arg in get_args(base)) if subscripted else ()
            out[origin] = _bind(origin, origin.__name__, params, args) if params else {}
            pending.append((origin, depth + int(subscripted)))
    return out


# only the selected fields are resolved, so an unresolvable annotation on a `ClassVar`, an `init=False` field or an
# extra annotation on a subclass does not fail the row
def _hints(t: type, keys: list[str], bindings: Bindings) -> dict[str, object]:
    declared = _declared(t)
    out: dict[str, object] = {}
    for key in keys:
        if key not in declared:
            continue
        klass, annotation = declared[key]
        try:
            out[key] = _subst(_resolve(klass, annotation, key), bindings.get(klass, {}))
        except NameError as err:
            msg = (
                f"{t.__name__}: field {key!r} names {err.name!r}, which is not available at runtime; "
                "import it outside `TYPE_CHECKING`"
            )
            raise InterfaceError(msg) from err
    return out


@dataclass(frozen=True, slots=True, kw_only=True)
class Spec:
    key: str
    required: bool
    names: tuple[str, ...] | None


def _is_initvar(t: type, key: str, declared: dict[str, tuple[type, object]]) -> bool:
    klass, annotation = declared.get(key, (t, None))
    try:
        resolved = _resolve(klass, annotation, key)
    except NameError:
        # `fields()` only drops a `ClassVar` or an `InitVar`, so an unresolvable one that is no `ClassVar` is an
        # `InitVar`, whatever alias it is spelled with
        text = annotation.__forward_arg__ if isinstance(annotation, ForwardRef) else annotation
        if isinstance(text, str):
            return CLASSVAR_TEXT.match(text) is None
        return annotation is not ClassVar and get_origin(annotation) is not ClassVar
    return resolved is InitVar or isinstance(resolved, InitVar)


def _dataclass_specs(t: type) -> list[Spec]:
    if not t.__dataclass_params__.init:  # ty: ignore[missing-attribute]
        raise _reject(t, "a dataclass with init=False is not supported")
    init_fields = fields(t)  # ty: ignore[invalid-argument-type]
    # `fields()` drops the `ClassVar` and `InitVar` pseudo-fields, which `__dataclass_fields__` still lists
    declared = _declared(t)
    for key in t.__dataclass_fields__.keys() - {f.name for f in init_fields}:  # ty: ignore[missing-attribute]
        if _is_initvar(t, key, declared):
            raise _reject(t, f"InitVar field {key!r} is not supported")
    return [
        Spec(key=f.name, required=f.default is MISSING and f.default_factory is MISSING, names=(f.name,))
        for f in init_fields
        if f.init
    ]


def _namedtuple_specs(t: type) -> list[Spec]:
    defaults = getattr(t, "_field_defaults", {})
    return [Spec(key=key, required=key not in defaults, names=(key,)) for key in t._fields]  # ty: ignore[missing-attribute]


def _typeddict_required(t: type, key: str, hint: object) -> bool:
    # `__required_keys__` is fixed at class creation from unresolved annotations, so it misses a qualifier written in a
    # quoted annotation or under `from __future__ import annotations`; the resolved qualifiers settle it
    required = key in t.__required_keys__  # ty: ignore[missing-attribute]
    for _ in range(MAX_ALIAS_DEPTH + 1):
        origin = get_origin(hint)
        qualifier = _qualifier(origin)
        if origin is not Annotated and qualifier is None:
            break
        if qualifier == "Required":
            required = True
        elif qualifier == "NotRequired":
            required = False
        hint = get_args(hint)[0]
    return required


def _typeddict_specs(t: type, hints: dict[str, object]) -> list[Spec]:
    return [Spec(key=key, required=_typeddict_required(t, key, hint), names=(key,)) for key, hint in hints.items()]


def _pydantic_rebuild(t: type, *, dataclass: bool, raise_errors: bool) -> None:
    # an explicit empty namespace stops pydantic using this frame's locals (`t`, `dataclass`) as the parent namespace;
    # the class still resolves names from its own module
    if dataclass:
        sys.modules["pydantic.dataclasses"].rebuild_dataclass(t, raise_errors=raise_errors, _types_namespace={})
    else:
        t.model_rebuild(raise_errors=raise_errors, _types_namespace={})  # ty: ignore[missing-attribute]


# a model whose forward references resolved after it was defined is rebuilt once here; the plan then holds the rebuilt
# validator
def _pydantic_complete(t: type, *, dataclass: bool) -> None:
    if t.__pydantic_complete__:  # ty: ignore[missing-attribute]
        return
    try:
        _pydantic_rebuild(t, dataclass=dataclass, raise_errors=False)
        if t.__pydantic_complete__:  # ty: ignore[missing-attribute]
            return
        _pydantic_rebuild(t, dataclass=dataclass, raise_errors=True)
    except Exception as err:
        name = getattr(err, "name", None) if isinstance(err, NameError) else None
        detail = f"it needs {name!r}" if name else str(err)
        hint = "; call model_rebuild() where the name is in scope" if name else ""
        msg = f"{t.__name__} is not fully defined ({detail}){hint}; define what its annotations name before the first query"
        raise InterfaceError(msg) from err
    msg = f"{t.__name__} is not fully defined; define what its annotations name before the first query"
    raise InterfaceError(msg)


def _pydantic_config(t: type, *, dataclass: bool) -> dict[str, object]:
    config = getattr(t, "__pydantic_config__", None) if dataclass else getattr(t, "model_config", None)
    return {str(k): v for k, v in config.items()} if isinstance(config, dict) else {}


# a before or wrap model validator anywhere in the MRO (pydantic merges the bases' decorators into the class's own)
# can rewrite the input, so no field's names can be known
def _pydantic_rewrites_input(t: type) -> bool:
    decorators = getattr(t, "__pydantic_decorators__", None)
    validators = getattr(decorators, "model_validators", {})
    return any(getattr(d.info, "mode", None) in {"before", "wrap"} for d in validators.values())


def _pydantic_name_flags(config: dict[str, object]) -> tuple[bool, bool]:
    # mirrors pydantic._internal._config._build_effective_config
    by_alias = config.get("validate_by_alias", True) is not False
    by_name = config.get("validate_by_name")
    if by_name is None:
        if (pbn := config.get("populate_by_name")) is not None:
            by_alias, by_name = True, bool(pbn)
        elif not by_alias:
            by_name = True
    return by_alias, bool(by_name)


def _pydantic_names(key: str, info: object, config: dict[str, object]) -> tuple[str, ...] | None:
    validation_alias = getattr(info, "validation_alias", None)
    alias = validation_alias if validation_alias is not None else getattr(info, "alias", None)
    if alias is not None and not isinstance(alias, str):
        return None
    by_alias, by_name = _pydantic_name_flags(config)
    if alias is None or not by_alias:
        return (key,)
    return (alias, key) if by_name and alias != key else (alias,)


def _pydantic_specs(t: type, bindings: Bindings, *, dataclass: bool) -> tuple[list[Spec], dict[str, object]]:
    if dataclass:
        # the decorator's `init` can only be `False`, which pydantic reads as "keep my validating `__init__`", so
        # there is no `init=False` to reject here; only an `InitVar` is
        init_fields = {f.name for f in fields(t)}  # ty: ignore[invalid-argument-type]
        declared = _declared(t)
        for key in t.__dataclass_fields__.keys() - init_fields:  # ty: ignore[missing-attribute]
            if _is_initvar(t, key, declared):
                raise _reject(t, f"InitVar field {key!r} is not supported")
    infos = t.__pydantic_fields__ if dataclass else t.model_fields  # ty: ignore[missing-attribute]
    config = _pydantic_config(t, dataclass=dataclass)
    unknown = _pydantic_rewrites_input(t)
    specs: list[Spec] = []
    hints: dict[str, object] = {}
    for key, info in infos.items():
        # only a pydantic dataclass's validator ignores an `init=False` field's input; a model still requires it
        if dataclass and info.init is False:
            continue
        names = None if unknown else _pydantic_names(key, info, config)
        specs.append(Spec(key=key, required=info.is_required(), names=names))
        # pydantic has already moved the `Annotated` metadata into the field, so this is the bare type; a model's
        # subscripted subclass has it resolved, but a pydantic dataclass keeps the declaring class's own parameters
        declaring = _declared(t).get(key, (t, None))[0] if dataclass else t
        hints[key] = _subst(info.annotation, bindings.get(declaring, {}))
    return specs, hints


def _specs(t: type, kind: str, bindings: Bindings) -> tuple[list[Spec], RowForm, Matching, dict[str, object]]:
    # a TypedDict's keys come from its annotations, so its hints are resolved before its requiredness is known
    if kind == "typeddict":
        hints = _hints(t, list(get_annotations(t, format=Format.FORWARDREF)), bindings)
        return _typeddict_specs(t, hints), "dict", "by_name", hints
    if kind == "namedtuple":
        specs, form, matching = _namedtuple_specs(t), "positional", "positional"
    else:
        specs, form, matching = _dataclass_specs(t), "keyword", "by_name"
    return specs, form, matching, _hints(t, [spec.key for spec in specs], bindings)


def _reject_pydantic(t: type) -> None:
    pydantic = sys.modules["pydantic"]
    if issubclass(t, pydantic.RootModel):
        raise _reject(t, "a pydantic RootModel is not a row type")
    v1 = sys.modules.get("pydantic.v1")
    if v1 is not None and issubclass(t, v1.BaseModel):
        raise _reject(t, "a pydantic v1 model is not supported")


# the class a generic row type names and the arguments it is bound with: a bare generic class takes its PEP 696
# defaults, and a pydantic model is replaced by its parametrized subclass, which pydantic has already resolved
def _bound_class(t: type, kind: str, args: tuple[object, ...] | None) -> tuple[type, dict[object, object], str]:
    params = getattr(t, "__parameters__", ())
    given = args if args is not None else ()
    if not params and not given:
        return t, {}, t.__name__
    mapping = _bind(t, t.__name__, params, given)
    if kind == "pydantic" and not _pydantic_dataclass(t):
        concrete = cast("type", t[tuple(mapping[param] for param in params)])  # ty: ignore[not-subscriptable]
        return concrete, {}, concrete.__name__
    return t, mapping, f"{t.__name__}[{', '.join(_type_text(mapping[param]) for param in params)}]"


def _field_plan(t: type, type_name: str, spec: Spec, hints: dict[str, object]) -> FieldPlan:
    if spec.key not in hints:
        raise _reject(
            t, f"field {spec.key!r} has no type annotation; use typing.NamedTuple, not collections.namedtuple"
        )
    try:
        annotation = _unwrap_field(hints[spec.key])
        nullable = _elem(annotation)
        flags = _value_flags(annotation)
    except _NameUnavailableError as err:
        msg = (
            f"{type_name}: field {spec.key!r} names {err.name!r}, which is not available at runtime; "
            "import it outside `TYPE_CHECKING`"
        )
        raise InterfaceError(msg) from err
    except InterfaceError as err:
        msg = f"{err} (field {spec.key!r} of {type_name})"
        raise InterfaceError(msg) from err
    return FieldPlan(key=spec.key, names=spec.names, nullable=nullable, flags=flags, required=spec.required)


def _named(t: type, kind: str, args: tuple[object, ...] | None = None) -> RowPlan:
    match kind:
        case "protocol":
            raise _reject(t, "a Protocol is not a row type")
    if kind == "pydantic":
        _reject_pydantic(t)
    t, mapping, type_name = _bound_class(t, kind, args)
    # pydantic has resolved a model's bases itself
    bindings = {t: mapping} if kind == "pydantic" and not _pydantic_dataclass(t) else _class_bindings(t, mapping)
    ctor: object = t
    validate = True
    if kind == "pydantic":
        dataclass = _pydantic_dataclass(t)
        _pydantic_complete(t, dataclass=dataclass)
        specs, hints = _pydantic_specs(t, bindings, dataclass=dataclass)
        form, matching = "validator", "by_name"
        # a model is validated with its validator, but a pydantic dataclass's validator rejects a dict under
        # `strict=True`, so the class is called with the row as keywords instead
        ctor, validate = (t, False) if dataclass else (t.__pydantic_validator__, True)  # ty: ignore[missing-attribute]
    else:
        specs, form, matching, hints = _specs(t, kind, bindings)
    if not specs:
        raise _reject(t, "has no fields")
    plans = [_field_plan(t, type_name, spec, hints) for spec in specs]
    fields = tuple(plans)
    return RowPlan(
        kind="named",
        arity=len(fields),
        nullable_mask=tuple(f.nullable for f in fields),
        class_flags=tuple(f.flags for f in fields),
        fields=fields,
        form=form,
        matching=matching,
        type_name=type_name,
        shape=ShapeSpec(
            ctor=ctor,
            form=form,
            matching=matching,
            type_name=type_name,
            fields=fields,
            validate=validate,
        ),
    )


def _reject_depth(t: object, hops: int) -> None:
    if hops >= MAX_ALIAS_DEPTH:
        raise _reject(t, f"alias chain deeper than {MAX_ALIAS_DEPTH}")


def _nullable_named(t: object) -> bool:
    if get_origin(t) not in {Union, types.UnionType}:
        return False
    members, has_none = _flatten(t)
    return has_none and len(members) == 1 and isinstance(members[0], type) and _named_form(members[0]) is not None


def _scalar(t: object) -> RowPlan:
    return RowPlan(
        kind="scalar",
        arity=1,
        nullable_mask=(_elem(t),),
        class_flags=(_value_flags(t),),
    )


def _build(t: object) -> RowPlan:
    t, hops = _unwrap_hops(t)
    if _is_none(t):
        return NO_ROWS
    if get_origin(t) is tuple:
        args = get_args(t)
        if not args or Ellipsis in args:
            raise _reject(t, "a tuple needs fixed arity of at least 1")
        mask = tuple(_elem(a) for a in args)
        spans = tuple(_value_flags(a) for a in args)
        return RowPlan(kind="tuple", arity=len(mask), nullable_mask=mask, class_flags=spans)
    # the leaves come before the named forms, so a value class that is a dataclass stays a scalar
    value_leaf = isinstance(t, type) and (_is_value_leaf(t) or _is_subscripted_value(t))
    if value_leaf or _is_subscripted_value(get_origin(t)):
        return _scalar(t)
    # a class reached through the full alias depth is one hop too deep, as the checker counts it
    if isinstance(t, type) and (kind := _named_form(t)) is not None:
        _reject_depth(t, hops)
        return _named(t, kind)
    # `G[int]` of a generic named class
    if isinstance(origin := get_origin(t), type) and (kind := _named_form(origin)) is not None:
        _reject_depth(t, hops)
        return _named(origin, kind, get_args(t))
    if _nullable_named(t):
        raise _reject(t, "a named row cannot be nullable; use .optional()")
    return _scalar(t)


def row_plan(t: object) -> RowPlan:
    try:
        return _cache[t]
    except KeyError:
        pass
    except TypeError:
        # unhashable annotation metadata: build without caching
        return _build(t)
    return _cache.setdefault(t, _build(t))

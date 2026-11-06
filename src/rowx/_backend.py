import importlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol, cast
from urllib.parse import urlparse

type NullReason = Literal[
    "NotNullColumn",
    "NullableColumn",
    "NotValidatedConstraint",
    "ForeignOrView",
    "ViewColumn",
    "OuterJoin",
    "GroupingSets",
    "ScanBoundary",
    "ReturningOldNew",
    "SetOperation",
    "ComputedExpr",
    "Computed",
    "ExplainFailed",
    "OverrideBang",
    "OverrideQuestion",
]
type IssueKind = Literal[
    "missing-column",
    "unexpected-column",
    "duplicate-column",
    "ambiguous-column",
    "column-name-mismatch",
    "arity-mismatch",
]


class BackendMissing(Exception):  # ruff: ignore[error-suffix-on-exception-name]
    pass


class UnsupportedSchemeError(Exception):
    pass


class ShapeIssue(Protocol):
    @property
    def kind(self) -> IssueKind: ...
    @property
    def columns(self) -> Sequence[int]: ...
    @property
    def fields(self) -> Sequence[int]: ...
    @property
    def name(self) -> str | None: ...
    @property
    def min(self) -> int | None: ...
    @property
    def max(self) -> int | None: ...
    @property
    def actual(self) -> int | None: ...


class ColumnInfo(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def type_name(self) -> str: ...
    @property
    def type_key(self) -> str | None: ...
    @property
    def nullable(self) -> bool: ...
    @property
    def reason(self) -> NullReason: ...
    @property
    def reason_detail(self) -> str | None: ...
    @property
    def element_key(self) -> str | None: ...
    @property
    def raw_name(self) -> str: ...


class ParamInfo(Protocol):
    @property
    def type_name(self) -> str: ...
    @property
    def type_key(self) -> str | None: ...
    @property
    def element_key(self) -> str | None: ...


class Describe(Protocol):
    @property
    def params(self) -> Sequence[ParamInfo]: ...
    @property
    def columns(self) -> Sequence[ColumnInfo]: ...


class Connection(Protocol):
    async def close(self) -> None: ...


type FieldSpec = tuple[Sequence[str] | None, bool]
type ConnectChecker = Callable[[str, float], Awaitable[Connection]]
type DescribeQuery = Callable[[Connection, str], Awaitable[Describe]]
type ParseDsn = Callable[[str], dict[str, Any]]
type MapColumns = Callable[
    [Sequence[str], Sequence[FieldSpec], bool],
    tuple[Sequence[int | None], Sequence[ShapeIssue]],
]


@dataclass(slots=True, kw_only=True, frozen=True)
class TypeEntry:
    matcher: str
    py_result: str
    alternatives: frozenset[str]
    accepted_params: tuple[str, ...]


@dataclass(slots=True, kw_only=True, frozen=True)
class CheckerBackend:
    connect_checker: ConnectChecker
    describe: DescribeQuery
    parse_dsn: ParseDsn
    map_columns: MapColumns
    types: Mapping[str, TypeEntry]
    param_classes: Mapping[str, str]
    value_leaves: Mapping[str, str]
    range_leaves: Mapping[str, str]
    range_classes: frozenset[str]
    value_classes: Mapping[str, str]


class _LoadedBackend(Protocol):
    def load(self) -> CheckerBackend: ...


# module path, then the extra name reported when that module is not installed
SCHEMES: Final[dict[str, tuple[str, str]]] = {
    "postgres": ("rowx.postgres._backend", "postgres"),
    "postgresql": ("rowx.postgres._backend", "postgres"),
}
DEFAULT_SCHEME: Final[str] = "postgres"
_LOADED: dict[str, CheckerBackend] = {}


def _parsed(token: str) -> str | None:
    # urlparse rejects some URLs (`postgresql://user@[broken`) before it yields a scheme; the scheme is still the
    # part before `://`, parsed on a placeholder so the result still comes from urllib.parse
    try:
        parsed = urlparse(token)
    except ValueError:
        head, sep, _rest = token.partition("://")
        if not sep or not head:
            return None
        try:
            parsed = urlparse(f"{head}://placeholder")
        except ValueError:
            return None
    if parsed.scheme and token.lower().startswith(f"{parsed.scheme}://"):
        return parsed.scheme
    return None


def _scheme(dsn: str) -> str | None:
    # the first token of a URL is `scheme://...`; urlparse lowercases it. A keyword DSN has no scheme.
    stripped = dsn.strip()
    if not stripped:
        return None
    return _parsed(stripped.split(None, 1)[0])


def _target(dsn: str) -> tuple[str, str]:
    scheme = _scheme(dsn)
    if scheme is None:
        scheme = DEFAULT_SCHEME
    if (target := SCHEMES.get(scheme)) is None:
        msg = f"unknown database scheme `{scheme}`"
        raise UnsupportedSchemeError(msg)
    return target


def _is_missing_backend(error: ModuleNotFoundError) -> bool:
    name = error.name
    return name is not None and (name == "rowx.postgres" or name.startswith("rowx.postgres."))


def _load(module_name: str, extra: str) -> CheckerBackend:
    if (loaded := _LOADED.get(module_name)) is not None:
        return loaded
    try:
        module = importlib.import_module(module_name)
        backend = cast("_LoadedBackend", module).load()
    except ModuleNotFoundError as error:
        if _is_missing_backend(error):
            msg = f"install `rowx[{extra}]`"
            raise BackendMissing(msg) from None
        raise
    _LOADED[module_name] = backend
    return backend


def module_for(dsn: str) -> str:
    return _target(dsn)[0]


def backend_for(dsn: str) -> CheckerBackend:
    module_name, extra = _target(dsn)
    return _load(module_name, extra)


def default_backend() -> CheckerBackend:
    module_name, extra = SCHEMES[DEFAULT_SCHEME]
    return _load(module_name, extra)

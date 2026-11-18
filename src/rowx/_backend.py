import importlib
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable
from urllib.parse import parse_qsl, urlparse

if TYPE_CHECKING:
    from rowx_core.backend import Backend


class BackendMissing(Exception):  # ruff: ignore[error-suffix-on-exception-name]
    pass


class UnsupportedSchemeError(Exception):
    pass


@runtime_checkable
class BackendModule(Protocol):
    def load(self) -> Backend: ...


SCHEMES: Final[dict[str, tuple[str, str]]] = {
    "postgres": ("rowx_postgres._backend", "postgres"),
    "postgresql": ("rowx_postgres._backend", "postgres"),
    "embedded": ("rowx_embedded._backend", "embedded"),
    "sqlite": ("rowx_sqlite._backend", "sqlite"),
}
SQLITE_HINT: Final[str] = "use `sqlite:///relative.db`, `sqlite:////absolute/path.db` or `sqlite::memory:`"
SQLITE_MEMORY: Final[str] = "sqlite::memory:"
SQLITE_SUFFIXES: Final[tuple[str, ...]] = (".db", ".sqlite", ".sqlite3", ".db3")
# query options that a SQLite URL may not carry, as name -> the values that are rejected (empty rejects any value)
SQLITE_REJECTED_OPTIONS: Final[dict[str, frozenset[str]]] = {
    "mode": frozenset({"memory"}),
    "immutable": frozenset(),
    "vfs": frozenset(),
    "cache": frozenset({"shared"}),
}
LOADED: Final[dict[str, Backend]] = {}


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


def _sqlite_error(problem: str) -> UnsupportedSchemeError:
    return UnsupportedSchemeError(f"{problem}; {SQLITE_HINT}")


def _check_sqlite_url(token: str) -> None:
    if token.lower() == SQLITE_MEMORY:
        return
    if not token.lower().startswith("sqlite://"):
        msg = "a SQLite URL needs `//` after the scheme"
        raise _sqlite_error(msg)
    try:
        parsed = urlparse(token)
        options = parse_qsl(parsed.query, keep_blank_values=True)
    except ValueError:
        msg = "the SQLite URL is not valid"
        raise _sqlite_error(msg) from None
    if parsed.netloc:
        msg = "a SQLite URL has no host"
        raise _sqlite_error(msg)
    if parsed.path in {"", "/"}:
        msg = "the SQLite URL has an empty path"
        raise _sqlite_error(msg)
    for name, value in options:
        rejected = SQLITE_REJECTED_OPTIONS.get(name.lower())
        if rejected is not None and (not rejected or value.lower() in rejected):
            msg = f"the SQLite URL option `{name}` is not supported"
            raise _sqlite_error(msg)


def _scheme(dsn: str) -> str | None:
    # the first token of a URL is `scheme://...`; urlparse lowercases it. A keyword DSN has no scheme.
    stripped = dsn.strip()
    if not stripped:
        return None
    token = stripped.split(None, 1)[0]
    # the embedded grammar also accepts `embedded:` with no `//`, which urlparse would not report as a scheme
    if token.lower().startswith("embedded:") and not token.lower().startswith("embedded://"):
        return "embedded"
    lowered = token.lower()
    if lowered.startswith("sqlite:"):
        _check_sqlite_url(token)
        return "sqlite"
    if lowered.startswith("file:"):
        msg = "a `file:` URI is not a SQLite target"
        raise _sqlite_error(msg)
    scheme = _parsed(token)
    # a keyword DSN holds `=` or several words; a lone token that names a database file is a path, not a DSN
    if scheme is None and stripped == token and "=" not in token and lowered.endswith(SQLITE_SUFFIXES):
        msg = "a bare file path is not a SQLite target"
        raise _sqlite_error(msg)
    return scheme


def _target(dsn: str) -> tuple[str, str]:
    # Keyword DSNs are the explicitly supported PostgreSQL target syntax.
    scheme = _scheme(dsn) or "postgres"
    if (target := SCHEMES.get(scheme)) is None:
        msg = f"unknown database scheme `{scheme}`"
        raise UnsupportedSchemeError(msg)
    return target


def module_for(dsn: str) -> str:
    return _target(dsn)[0]


def backend_for(dsn: str) -> Backend:
    module_name, extra = _target(dsn)
    if (loaded := LOADED.get(module_name)) is not None:
        return loaded
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        root = module_name.partition(".")[0]
        if error.name is not None and (error.name == root or error.name.startswith(f"{root}.")):
            msg = f"install `rowx[{extra}]`"
            raise BackendMissing(msg) from None
        raise
    if not isinstance(module, BackendModule):
        msg = f"invalid backend module `{module_name}`"
        raise TypeError(msg)
    backend = module.load()
    LOADED[module_name] = backend
    return backend

import importlib
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable
from urllib.parse import urlparse

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


def _scheme(dsn: str) -> str | None:
    # the first token of a URL is `scheme://...`; urlparse lowercases it. A keyword DSN has no scheme.
    stripped = dsn.strip()
    if not stripped:
        return None
    token = stripped.split(None, 1)[0]
    # the embedded grammar also accepts `embedded:` with no `//`, which urlparse would not report as a scheme
    if token.lower().startswith("embedded:") and not token.lower().startswith("embedded://"):
        return "embedded"
    return _parsed(token)


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

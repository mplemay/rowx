from typing import TYPE_CHECKING, Final

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from rowx_postgres._embedded import EmbeddedDatabase

# what a `server` test needs that one embedded session cannot give; a test names exactly one with `needs=`. `auth` is
# password authentication (the engine is trust-only) and `tcp` is a TCP host and port (the engine has a Unix socket)
SERVER_NEEDS: Final[frozenset[str]] = frozenset(
    {"sessions", "cancel", "tls", "create-database", "pg-stat-activity", "timing", "auth", "tcp"},
)
# where the embedded engine differs from Postgres; a test names exactly one with `divergence=` (a test that has several
# causes carries one mark for each), and unmarking it is the fix for that cause (a rowx codec fix, or an engine fix).
# `backend-pid` is the engine reporting one `pg_backend_pid()` for every session, so a test that tells a new connection
# from the old one by it cannot see the difference
ENGINE_DIVERGENCES: Final[frozenset[str]] = frozenset({"inet-cidr", "xml", "encoding", "locale", "backend-pid"})
# the only skips the embedded lane may take at run time: everything else that cannot run is marked `server`, so that an
# unmarked skip (a missing precondition nobody noticed) fails the lane instead of hiding a test
ALLOWED_RUNTIME_SKIPS: Final[frozenset[str]] = frozenset({"ROWX_FUZZ_REPORT is not 1"})
EMBEDDED_DATABASE_KEY: Final[pytest.StashKey[EmbeddedDatabase]] = pytest.StashKey()
SKIPPED_KEY: Final[pytest.StashKey[dict[str, str]]] = pytest.StashKey()
SERVER_KEY: Final[pytest.StashKey[dict[str, str]]] = pytest.StashKey()


# the SQL types the embedded engine cannot round-trip with rowx, and the divergence (see `ENGINE_DIVERGENCES` in the
# conftest) each one falls under; removing an entry unmarks every parametrisation of that type
DIVERGENT_TYPES: Final[dict[str, str]] = {"inet": "inet-cidr", "cidr": "inet-cidr", "xml": "xml"}


# marks the parametrisations whose SQL type diverges on the embedded engine (an array of one counts as that type)
def divergent[T](values: Iterable[T], sql_type: Callable[[T], str]) -> list[T | object]:
    marked: list[T | object] = []
    for value in values:
        if (cause := DIVERGENT_TYPES.get(sql_type(value).removesuffix("[]"))) is None:
            marked.append(value)
        else:
            marked.append(pytest.param(value, marks=pytest.mark.server(divergence=cause)))
    return marked

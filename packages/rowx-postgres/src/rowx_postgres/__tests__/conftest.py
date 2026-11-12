import gc
import os
import secrets
import time
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING, Final

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from rowx_postgres.__tests__.support.engine import (
    ALLOWED_RUNTIME_SKIPS,
    EMBEDDED_DATABASE_KEY,
    ENGINE_DIVERGENCES,
    SERVER_KEY,
    SERVER_NEEDS,
    SKIPPED_KEY,
)

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

ENGINE_MODULE: Final[str] = "rowx_embedded"
REQUIRE_ENV: Final[str] = "ROWX_REQUIRE_EMBEDDED"
TESTS_DIR: Final[Path] = Path(__file__).resolve().parent
SCHEMA_SQL: Final[Path] = TESTS_DIR / "support" / "schema.sql"
SEED_SQL: Final[Path] = TESTS_DIR / "support" / "seed_data.sql"
ROOT: Final[Path] = next(parent for parent in TESTS_DIR.parents if (parent / "Cargo.lock").is_file())
APP_SCHEMA_SQL: Final[Path] = ROOT / "examples" / "app" / "schema.sql"
SECURITY_SCHEMA_SQL: Final[Path] = ROOT / "examples" / "security" / "schema.sql"


def is_embedded_url(dsn: str) -> bool:
    return dsn.strip().lower().startswith("embedded:")


# an xdist controller only schedules; each worker starts its own instance
def _is_xdist_controller(config: pytest.Config) -> bool:
    distributed = bool(config.getoption("dist", default="no") != "no")
    return distributed and not hasattr(config, "workerinput")


# an embedded `ROWX_TEST_DSN` becomes the socket DSN of one instance for this process, before collection, because
# modules read the variable at import time
def pytest_configure(config: pytest.Config) -> None:
    config.stash[SKIPPED_KEY] = {}
    config.stash[SERVER_KEY] = {}
    dsn = os.environ.get("ROWX_TEST_DSN")
    if dsn is None or not is_embedded_url(dsn) or _is_xdist_controller(config):
        return
    if find_spec(ENGINE_MODULE) is None:
        message = f"ROWX_TEST_DSN is {dsn} but {ENGINE_MODULE} is not installed"
        raise pytest.UsageError(message)
    # the engine is optional, so nothing of it is imported unless the lane asks for it
    from rowx_embedded._instance import parse_embedded_url, start_blocking  # ruff: ignore[import-outside-top-level]

    target, _ = parse_embedded_url(dsn)
    database = start_blocking(target.directory)
    config.stash[EMBEDDED_DATABASE_KEY] = database
    os.environ["ROWX_TEST_DSN"] = database.dsn
    # the TLS lane has no embedded equivalent, and a stale value would point the TLS tests at another server
    os.environ.pop("ROWX_TEST_DSN_SSL", None)


# a test that leaves a session open starves every later test on its worker (53300), so the first one to do it is named
# and the rest fail with that one line instead of hundreds of unrelated errors
POISONED_KEY: Final[pytest.StashKey[str]] = pytest.StashKey()
PROBE_SECONDS: Final[float] = 3.0
TOO_MANY_CLIENTS: Final[str] = "too many clients"
RESOURCE_MARKERS: Final[tuple[str, ...]] = ("db", "embedded")


# one connect and close (about half a millisecond); the proxy waits out a session that is still being torn down, and a
# client that has just gone can look live for a moment, so a refusal collects garbage (a connection that only a cycle
# keeps alive closes then, which costs ten milliseconds and so is not done for every test) and is retried briefly
def _session_is_free(dsn: str) -> bool:
    deadline = time.monotonic() + PROBE_SECONDS
    collected = False
    while True:
        try:
            with psycopg.connect(dsn, connect_timeout=int(PROBE_SECONDS) + 1):
                return True
        except psycopg.OperationalError as error:
            if TOO_MANY_CLIENTS not in str(error):
                raise
            if not collected:
                gc.collect()
                collected = True
            elif time.monotonic() >= deadline:
                return False


def _reaches_the_instance(item: pytest.Item) -> bool:
    return "schema" in getattr(item, "fixturenames", ()) or any(
        next(item.iter_markers(name), None) is not None for name in RESOURCE_MARKERS
    )


# `rowx check` defaults to one describe session per cpu, and the instance serves one. Tests that ask for several with
# `--jobs` are marked `server`; the others run it through `main([...])` at dozens of call sites, so the default is
# pinned at its one seam for the length of each test (and `os.cpu_count`, which children also see, stays untouched).
@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item: pytest.Item) -> Iterator[object]:
    if EMBEDDED_DATABASE_KEY not in item.config.stash:
        return (yield)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("rowx.check.cli.default_jobs", lambda: 1)
        return (yield)


# after the test and every fixture of it: one connect proves the session was given back
@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item: pytest.Item) -> Iterator[None]:
    yield
    config = item.config
    if (database := config.stash.get(EMBEDDED_DATABASE_KEY, None)) is None or not _reaches_the_instance(item):
        return
    # a worker that is already poisoned has said so once, in the setup of this test
    if POISONED_KEY in config.stash:
        return
    if not _session_is_free(database.dsn):
        config.stash[POISONED_KEY] = item.nodeid
        pytest.fail(f"this test left a session open on the embedded database: {item.nodeid}", pytrace=False)


def pytest_unconfigure(config: pytest.Config) -> None:
    if (database := config.stash.get(EMBEDDED_DATABASE_KEY, None)) is not None:
        # the lane's instance is in memory, so there is nothing to wait for or keep
        database.abandon()


def _server_reason(item: pytest.Item) -> str | None:
    reasons: list[str] = []
    for marker in item.iter_markers("server"):
        needs, divergence = marker.kwargs.get("needs"), marker.kwargs.get("divergence")
        if (needs is None) == (divergence is None) or marker.args or set(marker.kwargs) - {"needs", "divergence"}:
            message = f"{item.nodeid}: server takes exactly one of needs= or divergence="
            raise pytest.UsageError(message)
        if needs is not None and needs not in SERVER_NEEDS:
            message = f"{item.nodeid}: server needs={needs!r} is not one of {sorted(SERVER_NEEDS)}"
            raise pytest.UsageError(message)
        if divergence is not None and divergence not in ENGINE_DIVERGENCES:
            message = f"{item.nodeid}: server divergence={divergence!r} is not one of {sorted(ENGINE_DIVERGENCES)}"
            raise pytest.UsageError(message)
        reasons.append(f"needs {needs}" if needs is not None else f"engine divergence {divergence}")
    return ", ".join(reasons) if reasons else None


# `timing` implies `server`, and runs first so that `-m` expressions see the implied marker
@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    embedded = EMBEDDED_DATABASE_KEY in config.stash or (
        (dsn := os.environ.get("ROWX_TEST_DSN")) is not None and is_embedded_url(dsn)
    )
    for item in items:
        if item.get_closest_marker("timing") is not None and item.get_closest_marker("server") is None:
            item.add_marker(pytest.mark.server(needs="timing"))
        if (reason := _server_reason(item)) is None:
            continue
        config.stash[SERVER_KEY][item.nodeid] = reason
        if embedded:
            message = f"needs a server: {reason}" if not reason.startswith("engine") else f"embedded {reason}"
            item.add_marker(pytest.mark.skip(reason=message))
            config.stash[SKIPPED_KEY][item.nodeid] = message


# on the embedded lane a test that skips at run time without being marked `server` (or listed in ALLOWED_RUNTIME_SKIPS)
# fails, so a precondition that went missing cannot quietly shrink what the lane runs
@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    config = item.config
    if not report.skipped or hasattr(report, "wasxfail") or EMBEDDED_DATABASE_KEY not in config.stash:
        return report
    if item.nodeid in config.stash[SKIPPED_KEY]:
        return report
    longrepr = report.longrepr
    reason = str(longrepr[2] if isinstance(longrepr, tuple) else longrepr).removeprefix("Skipped: ")
    if reason not in ALLOWED_RUNTIME_SKIPS:
        report.outcome = "failed"
        report.longrepr = (
            f"unmarked skip on the embedded lane ({reason}); mark the test server(needs=...) or fix the precondition"
        )
    return report


# a worker whose instance a test left occupied fails every later test with one line naming the culprit; and `embedded`
# tests need the companion: they skip without it, and fail instead when ROWX_REQUIRE_EMBEDDED=1
@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    if (culprit := item.config.stash.get(POISONED_KEY, None)) is not None:
        pytest.fail(f"this worker's embedded database is held by a session that {culprit} left open", pytrace=False)
    if item.get_closest_marker("embedded") is None or find_spec(ENGINE_MODULE) is not None:
        return
    if os.environ.get(REQUIRE_ENV) == "1":
        pytest.fail(f"{REQUIRE_ENV}=1 but {ENGINE_MODULE} is not installed")
    pytest.skip(f"{ENGINE_MODULE} is not installed")


def _require_dsn() -> str:
    if (dsn := os.environ.get("ROWX_TEST_DSN")) is not None and dsn:
        return dsn
    if os.environ.get("ROWX_REQUIRE_DB") == "1":
        pytest.fail("ROWX_REQUIRE_DB=1 but ROWX_TEST_DSN is not set")
    pytest.skip("ROWX_TEST_DSN is not set")


# accepts URL and keyword DSNs alike, and returns the keyword form
def _with_search_path(dsn: str, schema: str) -> str:
    return make_conninfo(dsn, options=f"-c search_path={schema}")


@pytest.fixture
def schema() -> Iterator[str]:
    admin_dsn = _require_dsn()
    name = f"t_{secrets.token_hex(8)}"
    # the admin connection closes before the yield, because an embedded database serves one session at a time
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        try:
            with conn.transaction():
                # citext lives in public and is shared, so concurrent loads must not race creating it
                conn.execute("SELECT pg_advisory_xact_lock(727)")
                conn.execute(sql.SQL("SET LOCAL search_path = {}").format(sql.Identifier(name)))
                conn.execute(SCHEMA_SQL.read_bytes())
                conn.execute(SEED_SQL.read_bytes())
        except BaseException:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
            raise
    try:
        yield _with_search_path(admin_dsn, name)
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


# a schema holding one example's tables, on the server `ROWX_TEST_DSN` names
def _example_schema(sql_path: Path) -> Iterator[str]:
    admin_dsn = _require_dsn()
    name = f"t_{secrets.token_hex(8)}"
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        try:
            with conn.transaction():
                conn.execute(sql.SQL("SET LOCAL search_path = {}").format(sql.Identifier(name)))
                conn.execute(sql_path.read_bytes())
        except BaseException:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
            raise
    try:
        yield _with_search_path(admin_dsn, name)
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


@pytest.fixture
def app_schema() -> Iterator[str]:
    yield from _example_schema(APP_SCHEMA_SQL)


@pytest.fixture
def security_schema() -> Iterator[str]:
    yield from _example_schema(SECURITY_SCHEMA_SQL)

import os
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Final

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

if TYPE_CHECKING:
    from collections.abc import Iterator

TESTS_DIR: Final[Path] = Path(__file__).resolve().parent
SCHEMA_SQL: Final[Path] = TESTS_DIR / "support" / "schema.sql"
SEED_SQL: Final[Path] = TESTS_DIR / "support" / "seed_data.sql"
ROOT: Final[Path] = next(parent for parent in TESTS_DIR.parents if (parent / "Cargo.lock").is_file())
APP_SCHEMA_SQL: Final[Path] = ROOT / "examples" / "app" / "schema.sql"
SECURITY_SCHEMA_SQL: Final[Path] = ROOT / "examples" / "security" / "schema.sql"


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
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        try:
            with conn.transaction():
                # citext lives in public and is shared, so concurrent loads must not race creating it
                conn.execute("SELECT pg_advisory_xact_lock(727)")
                conn.execute(sql.SQL("SET LOCAL search_path = {}").format(sql.Identifier(name)))
                conn.execute(SCHEMA_SQL.read_bytes())
                conn.execute(SEED_SQL.read_bytes())
            yield _with_search_path(admin_dsn, name)
        finally:
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
            yield _with_search_path(admin_dsn, name)
        finally:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


@pytest.fixture
def app_schema() -> Iterator[str]:
    yield from _example_schema(APP_SCHEMA_SQL)


@pytest.fixture
def security_schema() -> Iterator[str]:
    yield from _example_schema(SECURITY_SCHEMA_SQL)

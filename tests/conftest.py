import os
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Final
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

import psycopg
import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

TESTS_DIR: Final[Path] = Path(__file__).resolve().parent
SCHEMA_SQL: Final[Path] = TESTS_DIR / "schema.sql"
SEED_SQL: Final[Path] = TESTS_DIR / "seed_data.sql"


def _require_dsn() -> str:
    if dsn := os.environ.get("ROWX_TEST_DSN"):
        return dsn
    if os.environ.get("ROWX_REQUIRE_DB") == "1":
        pytest.fail("ROWX_REQUIRE_DB=1 but ROWX_TEST_DSN is not set")
    pytest.skip("ROWX_TEST_DSN is not set")


def _with_search_path(dsn: str, schema: str) -> str:
    parts = urlparse(dsn)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "options"]
    query.append(("options", f"-c search_path={schema}"))
    return urlunparse(parts._replace(query=urlencode(query, quote_via=quote)))


@pytest.fixture
def schema() -> Iterator[str]:
    admin_dsn = _require_dsn()
    name = f"t_{secrets.token_hex(8)}"
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{name}"')
        try:
            with conn.transaction():
                # citext lives in public and is shared, so concurrent loads must not race creating it
                conn.execute("SELECT pg_advisory_xact_lock(727)")
                conn.execute(f'SET LOCAL search_path = "{name}"')
                conn.execute(SCHEMA_SQL.read_text())
                conn.execute(SEED_SQL.read_text())
            yield _with_search_path(admin_dsn, name)
        finally:
            conn.execute(f'DROP SCHEMA "{name}" CASCADE')

import re
from pathlib import Path
from typing import Final

from rowx_postgres import RESET_QUERY

POOL_RS: Final[Path] = (
    next(parent for parent in Path(__file__).resolve().parents if (parent / "Cargo.lock").is_file())
    / "crates"
    / "rowx-postgres"
    / "src"
    / "pool.rs"
)


def test_the_python_and_rust_reset_queries_match() -> None:
    source = POOL_RS.read_text()
    found = re.search(r'pub const RESET_QUERY: &str =\s*"([^"]*)";', source)
    assert found is not None
    assert found.group(1) == RESET_QUERY


def test_the_reset_query_resets_authorization_first_and_qualifies_the_function() -> None:
    assert RESET_QUERY == (
        "RESET SESSION AUTHORIZATION; RESET ALL; SELECT pg_catalog.pg_advisory_unlock_all(); "
        "CLOSE ALL; UNLISTEN *; DISCARD TEMP;"
    )
    assert "DEALLOCATE" not in RESET_QUERY

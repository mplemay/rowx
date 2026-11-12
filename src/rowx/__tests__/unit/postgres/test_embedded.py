import sys

import rowx_postgres.embedded as backend_embedded

from rowx.postgres import embedded


def test_the_facade_exposes_the_database_of_the_backend() -> None:
    assert embedded is sys.modules["rowx.postgres.embedded"]
    assert embedded.Database is backend_embedded.Database
    assert embedded.__all__ == ["Database"]

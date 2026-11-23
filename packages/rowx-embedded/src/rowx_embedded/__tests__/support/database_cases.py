from typing import TYPE_CHECKING, reveal_type

from rowx_embedded import Database

if TYPE_CHECKING:
    from rowx_postgres import Pool


def takes_pool(pool: Pool) -> None: ...


def database_is_a_pool() -> None:
    database = Database()
    takes_pool(database)
    reveal_type(database.dsn)  # reveal: str
    reveal_type(database.directory)  # reveal: str | None
    reveal_type(Database("/var/lib/db").directory)  # reveal: str | None

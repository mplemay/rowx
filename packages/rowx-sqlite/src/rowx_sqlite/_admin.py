from pathlib import PurePath
from typing import TYPE_CHECKING, Final

from rowx_core.errors import InterfaceError

from rowx_sqlite._core import (
    DatabaseInfo,
    create_database,
    database_exists,
    database_ready,
    drop_database,
    list_databases,
)
from rowx_sqlite._options import ConnectOptions
from rowx_sqlite._scratch import scratch_url

if TYPE_CHECKING:
    from collections.abc import Sequence

    from rowx_core.errors import DatabaseError

# PostgreSQL's duplicate_database, which is what creating over an existing file raises
DUPLICATE_SQLSTATE: Final[str] = "42P04"


# a database is a file, so its name is the path the DSN gives; a memory DSN has none
class SqliteAdmin:
    def __init__(self, dsn: str) -> None:
        self._options = ConnectOptions.from_dsn(dsn)

    @property
    def database(self) -> str | None:
        return self._options.path

    def _name(self, name: str | None) -> str:
        if (chosen := name if name is not None else self._options.path) is None:
            msg = "an in-memory database has no name; use a file database URL"
            raise InterfaceError(msg)
        return chosen

    async def ready(self, seconds: float) -> None:
        await database_ready(self._name(None), seconds)

    async def exists(self, name: str | None = None) -> bool:
        return await database_exists(self._name(name))

    # a file has no comment to keep
    async def create(self, name: str | None = None, *, template: str | None = None, comment: str | None = None) -> None:
        del comment
        await create_database(self._name(name), template=template)

    async def create_scratch(self, name: str, *, comment: str) -> None:
        await self.create(name, comment=comment)

    # a file has no sessions to end: `force` only skips the check that no connection holds it open
    async def drop(self, name: str | None = None, *, force: bool = False) -> None:
        await drop_database(self._name(name), force=force)

    async def list(self, prefix: str) -> Sequence[DatabaseInfo]:
        return await list_databases(self._name(None), prefix)

    @staticmethod
    def with_database(name: str) -> str:
        return scratch_url(PurePath(name))

    @staticmethod
    def duplicate(error: DatabaseError) -> bool:
        return error.sqlstate == DUPLICATE_SQLSTATE

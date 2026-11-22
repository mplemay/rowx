from typing import TYPE_CHECKING, Final, override

from rowx_core.backend import CheckerSession
from rowx_core.errors import DatabaseError, InterfaceError

from rowx_sqlite._describe import describe

if TYPE_CHECKING:
    from rowx_core.backend import Describe, Failure

    from rowx_sqlite._core import Connection

# keyed by the SQLite result name, which is what `DatabaseError.code` carries
SQL_ERROR_HELP: Final[dict[str, str]] = {
    "SQLITE_READONLY": "the checker opens the database read-only, so it can only describe statements that read",
    "SQLITE_CANTOPEN": "check that the database file exists and that the path in the URL is correct",
    "SQLITE_BUSY": "another connection holds a lock on the database; retry once it is released",
    "SQLITE_LOCKED": "another connection holds a lock on the database; retry once it is released",
}
INTERRUPTED: Final[str] = "57014"
# plain SQL problems that carry no SQLite result code
GENERAL_ERROR: Final[str] = "HY000"
# BUSY and LOCKED, FULL, CANTOPEN, CORRUPT, NOTADB and READONLY: the environment, not the query, is at fault
OPERATIONAL: Final[frozenset[str]] = frozenset({"55P03", "53100", "58030", "XX001", "XX002", "25006"})


class SqliteCheckerSession(CheckerSession):
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    @override
    async def describe(self, sql: str) -> Describe:
        try:
            return await describe(self._connection, sql)
        except InterfaceError as error:
            # the describe raises this for SQL that is not one statement; only a closed connection is a lost one
            if self._connection.closed:
                raise
            raise DatabaseError(GENERAL_ERROR, str(error)) from error

    @override
    async def close(self) -> None:
        await self._connection.close()

    @override
    def classify(self, error: Exception) -> Failure:
        if isinstance(error, InterfaceError):
            return "lost"
        if isinstance(error, DatabaseError):
            if error.sqlstate == INTERRUPTED:
                return "timeout"
            if error.sqlstate in OPERATIONAL:
                return "operational"
        return "other"

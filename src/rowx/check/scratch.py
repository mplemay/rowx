import asyncio
import secrets
import sys
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from time import perf_counter
from typing import TYPE_CHECKING, Final
from urllib.parse import urlparse, urlunparse

from rowx.check.runner import CheckError, ScratchBuild, redact
from rowx.errors import DatabaseError, InterfaceError, MigrateError
from rowx.postgres import connect
from rowx.postgres._admin import create_database, drop_database

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from contextlib import AbstractAsyncContextManager

    from rowx.postgres import Migrator

PREFIX: Final[str] = "rowx_check_"
TEMPLATE: Final[str] = "template0"
INSUFFICIENT_PRIVILEGE: Final[str] = "42501"
CREATEDB_HELP: Final[str] = "`--scratch` needs a role with CREATEDB"
URL_SCHEMES: Final[tuple[str, ...]] = ("postgres://", "postgresql://")


# points `dsn` (a URL or a key=value string) at another database
def with_database(dsn: str, name: str) -> str:
    if not dsn.startswith(URL_SCHEMES):
        return f"{dsn} dbname={name}"
    parts = urlparse(dsn)
    # a `dbname` query parameter would override the path
    query = "&".join(pair for pair in parts.query.split("&") if pair and pair.split("=", 1)[0] != "dbname")
    return urlunparse(parts._replace(path=f"/{name}", query=query))


@dataclass(slots=True)
class Scratch:
    server_dsn: str
    name: str = field(default_factory=lambda: f"{PREFIX}{secrets.token_hex(8)}")
    # set before the create is attempted and cleared only by a drop that succeeded
    pending_drop: bool = False
    drop_error: str | None = None

    @property
    def leaked(self) -> bool:
        return self.pending_drop

    @property
    def url(self) -> str:
        return with_database(self.server_dsn, self.name)

    def session(self, migrator: Migrator) -> AbstractAsyncContextManager[ScratchBuild]:
        return self._session(migrator)

    @asynccontextmanager
    async def _session(self, migrator: Migrator) -> AsyncGenerator[ScratchBuild]:
        started = perf_counter()
        self.pending_drop = True
        try:
            await self._create()
            await self._migrate(migrator)
            yield ScratchBuild(url=self.url, seconds=perf_counter() - started)
        finally:
            await self.drop()

    async def _create(self) -> None:
        try:
            await create_database(self.server_dsn, self.name, template=TEMPLATE)
        except DatabaseError as error:
            if error.sqlstate == INSUFFICIENT_PRIVILEGE:
                msg = f"could not create the scratch database: {error.message}\nhelp: {CREATEDB_HELP}"
            else:
                msg = f"could not create the scratch database: {error}"
            raise CheckError(redact(msg, self.server_dsn)) from None
        except (InterfaceError, OSError) as error:
            msg = f"could not create the scratch database: {error}"
            raise CheckError(redact(msg, self.server_dsn)) from None

    async def _migrate(self, migrator: Migrator) -> None:
        try:
            # a normal (writable) connection that is closed before the checker session opens; nothing is printed
            async with await connect(self.url) as conn:
                await migrator.run(conn)
        except MigrateError as error:
            cause = error.__cause__
            msg = f"the migrations failed on the scratch database: {error}" + (f": {cause}" if cause else "")
            raise CheckError(redact(msg, self.server_dsn)) from None

    # a failed drop is recorded, not raised, so it never hides the error that ended the run
    async def drop(self) -> None:
        try:
            await drop_database(self.server_dsn, self.name, force=True)
        except (DatabaseError, InterfaceError, OSError) as error:
            self.drop_error = redact(str(error), self.server_dsn)
            return
        self.pending_drop = False
        self.drop_error = None

    # the last resort, from outside the event loop: a drop that was interrupted (a second Ctrl-C) is retried once
    def sweep(self) -> None:
        if not self.pending_drop:
            return
        with suppress(KeyboardInterrupt, Exception):
            asyncio.run(self.drop())
        if self.leaked:
            reason = f": {self.drop_error}" if self.drop_error is not None else ""
            sys.stderr.write(f"warning: could not drop the scratch database {self.name!r}{reason}\n")

import asyncio
import os
import secrets
import socket
import sys
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from time import perf_counter, time
from typing import TYPE_CHECKING, Final

from rowx.check.runner import CheckError, ScratchBuild, redact
from rowx.errors import DatabaseError, InterfaceError, MigrateError
from rowx.postgres import connect
from rowx.postgres._admin import create_database, drop_database, list_databases, with_database

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from contextlib import AbstractAsyncContextManager

    from rowx.postgres import Migrator

PREFIX: Final[str] = "rowx_check_"
TEMPLATE: Final[str] = "template0"
INSUFFICIENT_PRIVILEGE: Final[str] = "42501"
CREATEDB_HELP: Final[str] = "`--scratch` needs a role with CREATEDB"
TAG_PREFIX: Final[str] = "rowx:"
# a database tagged longer ago than this belongs to a run that is gone
ORPHAN_AGE_SECONDS: Final[int] = 3600


# `rowx:<pid>@<host>:<unix-ts>`
def make_tag() -> str:
    return f"{TAG_PREFIX}{os.getpid()}@{socket.gethostname()}:{int(time())}"


def tag_age(comment: str | None, now: float) -> float | None:
    if comment is None or not comment.startswith(TAG_PREFIX):
        return None
    # the host may contain a colon, so the timestamp is whatever follows the last one
    _, _, stamp = comment.rpartition(":")
    try:
        return now - int(stamp)
    except ValueError:
        return None


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
        try:
            return with_database(self.server_dsn, self.name)
        except InterfaceError, ValueError:
            # the parser's message can quote the DSN, so it is not repeated
            msg = "the database URL could not be parsed"
            raise CheckError(msg) from None

    def session(self, migrator: Migrator) -> AbstractAsyncContextManager[ScratchBuild]:
        return self._session(migrator)

    @asynccontextmanager
    async def _session(self, migrator: Migrator) -> AsyncGenerator[ScratchBuild]:
        started = perf_counter()
        # resolved before anything is created, so a URL that can't name the scratch database fails first
        url = self.url
        self.pending_drop = True
        try:
            await self.sweep_orphans()
            await self._create()
            await self._migrate(migrator, url)
            yield ScratchBuild(url=url, seconds=perf_counter() - started)
        finally:
            await self.drop()

    # best effort: a run that was SIGKILLed or cancelled by CI leaves its database behind; only an old tag with no
    # sessions is dropped, and any error (a listing that is refused, a database another role owns) is ignored
    async def sweep_orphans(self) -> None:
        with suppress(DatabaseError, InterfaceError, OSError):
            found = await list_databases(self.server_dsn, PREFIX)
            now = time()
            for info in found:
                age = tag_age(info.comment, now)
                if info.name == self.name or age is None or age <= ORPHAN_AGE_SECONDS or info.sessions > 0:
                    continue
                with suppress(DatabaseError, InterfaceError, OSError):
                    await drop_database(self.server_dsn, info.name)

    async def _create(self) -> None:
        try:
            await create_database(self.server_dsn, self.name, template=TEMPLATE, comment=make_tag())
        except DatabaseError as error:
            if error.sqlstate == INSUFFICIENT_PRIVILEGE:
                msg = f"could not create the scratch database: {error.message}\nhelp: {CREATEDB_HELP}"
            else:
                msg = f"could not create the scratch database: {error}"
            raise CheckError(redact(msg, self.server_dsn)) from None
        except (InterfaceError, OSError) as error:
            msg = f"could not create the scratch database: {error}"
            raise CheckError(redact(msg, self.server_dsn)) from None

    async def _migrate(self, migrator: Migrator, url: str) -> None:
        try:
            # a normal (writable) connection that is closed before the checker session opens; nothing is printed
            async with await connect(url) as conn:
                await migrator.run(conn)
        except MigrateError as error:
            cause = error.__cause__
            msg = f"the migrations failed on the scratch database: {error}" + (f": {cause}" if cause else "")
            raise CheckError(redact(msg, self.server_dsn)) from None
        except (DatabaseError, InterfaceError, OSError) as error:
            msg = f"could not migrate the scratch database: {error}"
            raise CheckError(redact(redact(msg, url), self.server_dsn)) from None

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

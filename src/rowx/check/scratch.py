import asyncio
import secrets
import sys
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from time import perf_counter, time
from typing import TYPE_CHECKING, Final

from rowx_core.errors import DatabaseError, Error, InterfaceError, MigrateError
from rowx_core.lifecycle import ORPHAN_AGE_SECONDS, make_tag, tag_age

from rowx.check.runner import CheckError, ScratchBuild, redact

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from contextlib import AbstractAsyncContextManager

    from rowx_core.backend import AdminService, MigrationService

PREFIX: Final[str] = "rowx_check_"


@dataclass(slots=True)
class Scratch:
    server_dsn: str
    admin: AdminService
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
            return self.admin.with_database(self.name)
        except InterfaceError, ValueError:
            # the parser's message can quote the DSN, so it is not repeated
            msg = "the database URL could not be parsed"
            raise CheckError(msg) from None

    def session(self, migrator: MigrationService) -> AbstractAsyncContextManager[ScratchBuild]:
        return self._session(migrator)

    @asynccontextmanager
    async def _session(self, migrator: MigrationService) -> AsyncGenerator[ScratchBuild]:
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
        with suppress(Error, OSError):
            admin = self.admin
            found = await admin.list(PREFIX)
            now = time()
            for info in found:
                age = tag_age(info.comment, now)
                if info.name == self.name or age is None or age <= ORPHAN_AGE_SECONDS or info.sessions > 0:
                    continue
                with suppress(Error, OSError):
                    await admin.drop(info.name)

    async def _create(self) -> None:
        try:
            await self.admin.create_scratch(self.name, comment=make_tag())
        except (DatabaseError, InterfaceError, OSError) as error:
            msg = f"could not create the scratch database: {error}"
            raise CheckError(redact(msg, self.server_dsn)) from None

    async def _migrate(self, migrator: MigrationService, url: str) -> None:
        try:
            # a normal (writable) connection that is closed before the checker session opens; nothing is printed
            await migrator.run_on(url)
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
            await self.admin.drop(self.name, force=True)
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

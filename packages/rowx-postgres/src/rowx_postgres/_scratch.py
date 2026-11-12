import asyncio
import sys
from contextlib import asynccontextmanager, suppress
from time import perf_counter
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from rowx_core.backend import ScratchBuild, ScratchService
from rowx_core.errors import InterfaceError

from rowx_postgres._embedded import (
    EmbeddedDatabase,
    is_embedded_url,
    parse_embedded_url,
    start_from_snapshot,
    start_owned,
    take_snapshot,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence
    from contextlib import AbstractAsyncContextManager

    from rowx_core.backend import EmbeddedService, MigrationService


# the instance's socket DSN with the other parameters of the embedded URL (`options=-c search_path=x`) added to it
def socket_url(dsn: str, query: str) -> str:
    if not query:
        return dsn
    parts = urlsplit(dsn)
    merged = urlencode([*parse_qsl(parts.query, keep_blank_values=True), *parse_qsl(query, keep_blank_values=True)])
    return urlunsplit(parts._replace(query=merged))


async def close_all(databases: Sequence[EmbeddedDatabase]) -> list[BaseException]:
    # every close is attempted, and a failed one is reported rather than raised over the error that ended the run
    results = await asyncio.gather(*(database.close() for database in databases), return_exceptions=True)
    return [result for result in results if isinstance(result, BaseException)]


class EmbeddedScratch:
    def __init__(self, query: str = "") -> None:
        self._query = query
        # every instance this scratch started that has not been closed yet; recorded as soon as it exists, so a run
        # that is interrupted anywhere can still stop it
        self._open: list[EmbeddedDatabase] = []
        self.close_error: str | None = None

    @property
    def leaked(self) -> bool:
        return any(not database.closed for database in self._open)

    def session(self, migrator: MigrationService, workers: int) -> AbstractAsyncContextManager[ScratchBuild]:
        if workers < 1:
            msg = "an embedded scratch needs at least one worker"
            raise ValueError(msg)
        return self._session(migrator, workers)

    async def _build(self, migrator: MigrationService, workers: int) -> list[str]:
        first = await start_owned(None)
        self._open.append(first)
        first_url = socket_url(first.dsn, self._query)
        await self._migrate(migrator, first_url)
        urls = [first_url]
        if workers > 1:
            # the migrated instance is the first worker; the others boot from its snapshot, which is taken once
            snapshot = await take_snapshot(first)
            urls.extend(await self._boot(snapshot, workers - 1))
        return urls

    @asynccontextmanager
    async def _session(self, migrator: MigrationService, workers: int) -> AsyncGenerator[ScratchBuild]:
        started = perf_counter()
        try:
            urls = await self._build(migrator, workers)
            yield ScratchBuild(urls=tuple(urls), seconds=perf_counter() - started)
        except asyncio.CancelledError, KeyboardInterrupt:
            # an interrupted run wants nothing of a memory database, and a close would wait for the engine for as long
            # as a statement runs
            self.abandon()
            raise
        finally:
            await self.close()

    # the migration runs as a task of its own so that an interrupt can stop the instances first: the migrator's
    # connection then ends at once, instead of settling behind a statement that the engine cannot cancel
    async def _migrate(self, migrator: MigrationService, url: str) -> None:
        task = asyncio.ensure_future(migrator.run_on(url))
        # a result nobody awaits any more is not an unretrieved exception
        task.add_done_callback(lambda done: done.cancelled() or done.exception())
        try:
            await asyncio.wait({task})
        except BaseException:
            self.abandon()
            task.cancel()
            raise
        task.result()

    async def _boot(self, snapshot: bytes, count: int) -> list[str]:
        results = await asyncio.gather(*(start_from_snapshot(snapshot) for _ in range(count)), return_exceptions=True)
        urls: list[str] = []
        failure: BaseException | None = None
        for result in results:
            if isinstance(result, EmbeddedDatabase):
                self._open.append(result)
                urls.append(socket_url(result.dsn, self._query))
            elif failure is None:
                failure = result
        if failure is not None:
            raise failure
        return urls

    def abandon(self) -> None:
        for database in self._open:
            with suppress(Exception):
                database.abandon()

    # a failed close is recorded, not raised, so it never hides the error that ended the run
    async def close(self) -> None:
        errors = await close_all([database for database in self._open if not database.closed])
        self.close_error = str(errors[0]) if errors else None

    # the last resort, from outside the event loop: whatever is still open is abandoned, never closed, because a close
    # waits for the engine for as long as its statement runs and a second Ctrl-C must not wait
    def sweep(self) -> None:
        if not (remaining := [database for database in self._open if not database.closed]):
            return
        self.abandon()
        if any(not database.closed for database in remaining):
            sys.stderr.write("warning: could not stop the embedded scratch database\n")


class PostgresEmbedded:
    def __init__(self, url: str) -> None:
        self._target, self._query = parse_embedded_url(url)
        self._database: EmbeddedDatabase | None = None

    @property
    def persistent(self) -> bool:
        return self._target.directory is not None

    def open(self) -> AbstractAsyncContextManager[str]:
        return self._open()

    @asynccontextmanager
    async def _open(self) -> AsyncGenerator[str]:
        if self._target.directory is None:
            msg = "an in-memory embedded database is gone when the command exits"
            raise InterfaceError(msg)
        # started once, here, and never inside a connection retry: a directory that another process owns fails at once
        database = await start_owned(self._target.directory)
        self._database = database
        try:
            yield socket_url(database.dsn, self._query)
        finally:
            await database.close()

    def scratch(self) -> ScratchService:
        return EmbeddedScratch(self._query)

    # a directory that is still open when the run is torn down (a second Ctrl-C while its close waits for a running
    # statement) is abandoned: it is left as a crash would leave it, and it runs with fsync on, so it opens again
    def sweep(self) -> None:
        if (database := self._database) is not None and not database.closed:
            with suppress(Exception):
                database.abandon(force=True)


def embedded_service(url: str) -> EmbeddedService | None:
    return PostgresEmbedded(url) if is_embedded_url(url) else None


__all__ = ["EmbeddedScratch", "PostgresEmbedded", "embedded_service", "socket_url"]

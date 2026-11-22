import asyncio
import os
import secrets
import tempfile
from contextlib import asynccontextmanager, suppress
from pathlib import Path, PurePath
from time import perf_counter, time
from typing import TYPE_CHECKING, Final
from urllib.parse import quote

from rowx_core import query_unchecked
from rowx_core.backend import ScratchBuild
from rowx_core.errors import InterfaceError
from rowx_core.lifecycle import ORPHAN_AGE_SECONDS

from rowx_sqlite._connection import connect
from rowx_sqlite._options import ConnectOptions

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from contextlib import AbstractAsyncContextManager

    from rowx_core.backend import MigrationService

PREFIX: Final[str] = "rowx_check_"
SUFFIX: Final[str] = ".db"
SIDECARS: Final[tuple[str, ...]] = ("", "-wal", "-shm", "-journal")
FILE_MODE: Final[int] = 0o600


# `sqlite:////abs/path` for a POSIX path and `sqlite:///C:/path` for a drive path
def scratch_url(path: PurePath) -> str:
    return f"sqlite:///{quote(path.as_posix(), safe='/:')}"


# an in-memory database is invisible to every other connection, so the scratch is one temporary file that all the
# describe workers share
class SqliteScratch:
    def __init__(self, _dsn: str, /, *, sweep_previous: bool = True) -> None:
        # the DSN names the target of the run; the scratch file never depends on it
        self._sweep_previous = sweep_previous
        self._path = Path(tempfile.gettempdir()) / f"{PREFIX}{secrets.token_hex(8)}{SUFFIX}"
        self._pending = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def url(self) -> str:
        return scratch_url(self._path)

    def session(self, migrator: MigrationService, workers: int = 1) -> AbstractAsyncContextManager[ScratchBuild]:
        if workers < 1:
            msg = "a scratch database needs at least one worker"
            raise ValueError(msg)
        return self._session(migrator)

    @asynccontextmanager
    async def _session(self, migrator: MigrationService) -> AsyncGenerator[ScratchBuild]:
        started = perf_counter()
        self._pending = True
        try:
            if self._sweep_previous:
                await asyncio.to_thread(self._sweep_orphans)
            await asyncio.to_thread(self._create)
            await migrator.run_on(self.url)
            # a writable connection leaves the file in WAL mode, which a `mode=ro` reader cannot open without a
            # writable `-shm`; the rollback journal has no such file
            await self._rollback_journal()
            yield ScratchBuild(urls=(self.url,), seconds=perf_counter() - started)
        finally:
            await asyncio.to_thread(self._remove)

    def _create(self) -> None:
        # exclusive, so a name that is already taken is an error instead of someone else's database
        os.close(os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, FILE_MODE))

    async def _rollback_journal(self) -> None:
        # the open itself switches a file to WAL, so the switch back comes after it; the connection is closed before
        # any worker opens the file
        async with await connect(ConnectOptions(path=str(self._path), mode="rw")) as connection:
            mode = await query_unchecked[str](t"PRAGMA journal_mode = DELETE").one(connection)
        if mode.lower() != "delete":
            msg = f"could not switch the scratch database to the rollback journal; it stays in {mode} mode"
            raise InterfaceError(msg)

    def _remove(self) -> None:
        for suffix in SIDECARS:
            with suppress(OSError):
                Path(f"{self._path}{suffix}").unlink(missing_ok=True)
        if not self._path.exists():
            self._pending = False

    # best effort: a run that was killed leaves its file behind; only an old one is removed
    def _sweep_orphans(self) -> None:
        now = time()
        with suppress(OSError):
            for found in self._path.parent.glob(f"{PREFIX}*{SUFFIX}"):
                if found == self._path:
                    continue
                with suppress(OSError):
                    if now - found.stat().st_mtime > ORPHAN_AGE_SECONDS:
                        for suffix in SIDECARS:
                            Path(f"{found}{suffix}").unlink(missing_ok=True)

    # the last resort, from outside the event loop
    def sweep(self) -> None:
        if self._pending:
            self._remove()

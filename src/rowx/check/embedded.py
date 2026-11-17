import asyncio
import signal
import sys
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from typing import TYPE_CHECKING, Final
from urllib.parse import parse_qsl, urlsplit

from rowx_core.backend import EmbeddedTargetError
from rowx_core.errors import DatabaseError, InterfaceError, MigrateError

from rowx._backend import BackendMissing, UnsupportedSchemeError, backend_for
from rowx.check.runner import CheckError, redact

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Sequence

    from rowx_core.backend import EmbeddedService, MigrationService, ScratchBuild, ScratchService


WAITING: Final[str] = (
    "interrupted: waiting for the running statement to finish; the embedded engine cannot cancel it "
    "(press Ctrl-C again to abort)\n"
)


# the directory that holds the Unix socket of an instance's DSN, which a driver message can quote on its own
def socket_directory(url: str) -> str | None:
    try:
        pairs = parse_qsl(urlsplit(url).query)
    except ValueError:
        return None
    return next((value for key, value in pairs if key == "host" and value.startswith("/")), None)


# `redact` for every URL that the run owns, and the bare socket directory that a driver message can quote on its own
def redact_owned(text: str, dsn: str, owned: Sequence[str]) -> str:
    for url in (dsn, *owned):
        text = redact(text, url)
    for url in owned:
        if (directory := socket_directory(url)) is not None:
            text = text.replace(directory, "<socket directory>")
    return text


# the service for an embedded URL, or None for any other URL (an unknown scheme or a missing backend is reported
# later, by the code that needs the backend)
def embedded_for(dsn: str | None) -> EmbeddedService | None:
    if dsn is None or not dsn.strip():
        return None
    try:
        backend = backend_for(dsn)
    except UnsupportedSchemeError, BackendMissing:
        return None
    try:
        return backend.embedded(dsn)
    except EmbeddedTargetError as error:
        # the factory's own refusals never quote the URL
        msg = f"the embedded database URL is not valid: {error}"
        raise CheckError(msg) from None
    except InterfaceError, ValueError:
        # the parser's message can quote the URL, so it is not repeated
        msg = "the database URL could not be parsed"
        raise CheckError(msg) from None


# the loop's own handler cancels the main task, which then waits behind the engine until its statement ends, and says
# nothing meanwhile. Once an instance is up, the first Ctrl-C says so at once and cancels the task as before; the
# second stops the instance without waiting (a crash-safe directory is left as a crash leaves it) and leaves the loop
def _install_interrupts(service: EmbeddedService, interrupted: list[bool], aborted: list[bool]) -> Callable[[], None]:
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()

    def abort() -> None:
        aborted.append(True)
        service.sweep()
        raise KeyboardInterrupt

    def first() -> None:
        interrupted.append(True)
        sys.stderr.write(WAITING)
        sys.stderr.flush()
        loop.add_signal_handler(signal.SIGINT, abort)
        if task is not None:
            task.cancel()

    def remove() -> None:
        with suppress(NotImplementedError, RuntimeError, ValueError):
            loop.remove_signal_handler(signal.SIGINT)

    try:
        loop.add_signal_handler(signal.SIGINT, first)
    except NotImplementedError, RuntimeError, ValueError:
        # not available off the main thread or on every platform: the loop's own handling then applies
        return lambda: None
    return remove


# starts the instance of an embedded directory once; a failure to start (a directory another process owns, a missing
# companion) is final, so it is raised here and never reaches a connection retry
@asynccontextmanager
async def open_instance(service: EmbeddedService) -> AsyncGenerator[str]:
    removers: list[Callable[[], None]] = []
    async with AsyncExitStack() as stack:
        # registered first, so the handlers stay until the instance has closed: that close is what waits
        stack.callback(lambda: [remove() for remove in removers])
        try:
            url = await stack.enter_async_context(service.open())
        except (InterfaceError, DatabaseError, OSError) as error:
            msg = f"could not start the embedded database: {error}"
            raise CheckError(msg) from None
        interrupted: list[bool] = []
        aborted: list[bool] = []
        removers.append(_install_interrupts(service, interrupted, aborted))
        try:
            yield url
        except asyncio.CancelledError:
            # the loop's own handler turns its cancellation into a KeyboardInterrupt, but this one was ours
            # an abort already left the loop with a KeyboardInterrupt; this is only its tasks being cancelled
            if interrupted and not aborted:
                raise KeyboardInterrupt from None
            raise


# the same messages as the server scratch, so a migration that fails reads the same on either backend
@asynccontextmanager
async def scratch_session(
    scratch: ScratchService, migrator: MigrationService, workers: int
) -> AsyncGenerator[ScratchBuild]:
    async with AsyncExitStack() as stack:
        try:
            build = await stack.enter_async_context(scratch.session(migrator, workers))
        except MigrateError as error:
            cause = error.__cause__
            msg = f"the migrations failed on the scratch database: {error}" + (f": {cause}" if cause else "")
            raise CheckError(msg) from None
        except (DatabaseError, InterfaceError, OSError) as error:
            msg = f"could not build the embedded scratch database: {error}"
            raise CheckError(msg) from None
        yield build

import os
import sys
from atexit import register
from contextlib import suppress
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from types import TracebackType

# the distribution is installed only where an engine wheel exists; a wheel whose engine will not load is a broken install
try:
    from rowx_embedded import _engine
except ImportError as error:
    msg = f"the embedded engine could not be loaded: {error}"
    raise ImportError(msg) from error

# the PostgreSQL release the engine implements (the package version is `<ENGINE_VERSION>.<revision>`)
ENGINE_VERSION: Final[str] = _engine.ENGINE_VERSION
# the engine runtime and wasm module the snapshots are bound to
RUNTIME_VERSION: Final[str] = _engine.RUNTIME_VERSION
WASM_SHA256: Final[str] = _engine.WASM_SHA256
# the snapshot container layout, and the contrib extensions every instance has; both belong in a snapshot cache key
SNAPSHOT_FORMAT: Final[int] = _engine.SNAPSHOT_FORMAT
EXTENSIONS: Final[tuple[str, ...]] = _engine.EXTENSIONS

# An abandoned instance can leave its engine thread inside a long statement, in JIT-compiled code. Letting the
# interpreter finalize then runs libc's exit-time teardown under that thread, and the process dies by SIGSEGV instead of
# exiting with its status. So a process with such a thread leaves with `os._exit` after flushing. Entry points that know
# their status (the `rowx` CLI) call `exit_if_abandoned(code)` themselves; for everything else, the `atexit` fallback
# below uses the status it can know: 1 after an uncaught exception, else the code recorded by `set_exit_code`, else 0.
_exit_code: int = 0
INTERRUPT_EXIT_CODE: Final[int] = 130


def set_exit_code(code: int) -> None:
    global _exit_code  # ruff: ignore[global-statement]
    _exit_code = code


def exit_if_abandoned(code: int = 0) -> None:
    if not _engine.abandoned_running():
        return
    # live instances still get their clean close (it never waits long: see `join_background_closes`)
    _engine.close_all()
    for stream in (sys.stdout, sys.stderr):
        with suppress(OSError, ValueError):
            stream.flush()
    os._exit(code)


def _at_exit() -> None:
    exit_if_abandoned(_exit_code)


_previous_hook = sys.excepthook


def _record_uncaught(kind: type[BaseException], value: BaseException, trace: TracebackType | None) -> None:
    # an interrupt exits as a shell reports one (128 + SIGINT), as the interpreter's own exit would
    set_exit_code(INTERRUPT_EXIT_CODE if issubclass(kind, KeyboardInterrupt) else 1)
    _previous_hook(kind, value, trace)


sys.excepthook = _record_uncaught

# registered before `close_all`, so it runs after it (handlers run last in, first out)
register(_at_exit)
# an instance the program forgot to close is closed at exit (a forked child's exit leaves the parent's alone)
register(_engine.close_all)

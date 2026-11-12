from atexit import register
from typing import Final

from rowx_embedded import _engine

# bumped when the Python surface of this module changes incompatibly; `rowx_postgres` checks it on first use
API_VERSION: Final[int] = 1
# the PostgreSQL release the engine implements (the package version is `<ENGINE_VERSION>.<revision>`)
ENGINE_VERSION: Final[str] = _engine.ENGINE_VERSION
# the engine runtime and wasm module the snapshots are bound to
RUNTIME_VERSION: Final[str] = _engine.RUNTIME_VERSION
WASM_SHA256: Final[str] = _engine.WASM_SHA256
# the snapshot container layout, and the contrib extensions every instance has; both belong in a snapshot cache key
SNAPSHOT_FORMAT: Final[int] = _engine.SNAPSHOT_FORMAT
EXTENSIONS: Final[tuple[str, ...]] = _engine.EXTENSIONS

# an instance the program forgot to close is closed at exit (a forked child's exit leaves the parent's alone)
register(_engine.close_all)

from rowx_core.backend import Backend

from rowx_sqlite._services import SqliteMigrations, in_memory


def load() -> Backend:
    return Backend(migration_factory=SqliteMigrations, memory_factory=in_memory)

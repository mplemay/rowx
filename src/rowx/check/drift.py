from collections import Counter
from typing import TYPE_CHECKING, Final

from rowx._backend import DatabaseConnection, MigrationStatus, Migrator, backend_for
from rowx.check.migrate import load_migrator, resolve_target
from rowx.check.runner import CheckError, redact
from rowx.errors import DatabaseError, InterfaceError, MigrateError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from rowx.check.config import Config

DRIFT_STATES: Final[tuple[str, ...]] = ("pending", "modified", "missing", "dirty")
MAX_VERSIONS: Final[int] = 5
INSUFFICIENT_PRIVILEGE: Final[str] = "42501"
HELP: Final[str] = "run `rowx migrate run`, or `rowx check --scratch`"


# its message holds no database URL, so it is not redacted (a password such as "rowx" would mangle the help line)
class DriftError(CheckError):
    pass


def summarize(statuses: Sequence[MigrationStatus], directory: str, *, ignore_missing: bool = False) -> str | None:
    # `ignore_missing` lets `missing` pass
    ignored = {"missing"} if ignore_missing else set()
    by_state: dict[str, list[int]] = {state: [] for state in DRIFT_STATES}
    for status in statuses:
        if status.state in by_state and status.state not in ignored:
            by_state[status.state].append(status.version)
    counts = Counter({state: len(versions) for state, versions in by_state.items() if versions})
    if not counts:
        return None
    lines = [f"database has drifted from `{directory}/`:"]
    for state in DRIFT_STATES:
        if state not in counts:
            continue
        versions = sorted(by_state[state])
        shown = ", ".join(str(version) for version in versions[:MAX_VERSIONS])
        more = ", ..." if len(versions) > MAX_VERSIONS else ""
        lines.append(f"  {counts[state]} {state}: {shown}{more}")
    lines.append(HELP)
    return "\n".join(lines)


def _denied(error: BaseException | None) -> bool:
    return isinstance(error, DatabaseError) and error.sqlstate == INSUFFICIENT_PRIVILEGE


# the table is named as configured, so it is not a secret and the message is not redacted
def _grant_hint(table: str) -> str:
    return f"could not read the migration history: permission denied; grant SELECT on {table} to the checker role"


async def read_history(migrator: Migrator, conn: DatabaseConnection) -> Sequence[MigrationStatus]:
    # `info` creates nothing, so it runs as is in the read-only checker session
    return await migrator.info(conn)


# returns the number of migrations checked, or None when the project has no `[tool.rowx.migrate]`
async def check_drift(config: Config, dsn: str) -> int | None:
    if config.migrate is None:
        return None
    target = resolve_target(config, None)
    migrator = load_migrator(target, dsn)
    try:
        conn = await backend_for(dsn).migrate.open_checker(dsn, config.statement_timeout_ms / 1000)
    except (InterfaceError, DatabaseError, OSError) as error:
        msg = f"could not connect to the database: {redact(str(error), dsn)}"
        raise CheckError(msg) from None
    try:
        statuses = await read_history(migrator, conn)
    except MigrateError as error:
        cause = error.__cause__
        if _denied(error) or _denied(cause):
            raise CheckError(_grant_hint(target.migrate.table)) from None
        msg = f"could not read the migration history: {error}" + (f": {cause}" if cause is not None else "")
        raise CheckError(redact(msg, dsn)) from None
    except (InterfaceError, DatabaseError, OSError) as error:
        if _denied(error):
            raise CheckError(_grant_hint(target.migrate.table)) from None
        msg = f"could not read the migration history: {redact(str(error), dsn)}"
        raise CheckError(msg) from None
    finally:
        await conn.close()
    if (summary := summarize(statuses, target.migrate.dir, ignore_missing=target.migrate.ignore_missing)) is not None:
        raise DriftError(summary)
    return len(statuses)

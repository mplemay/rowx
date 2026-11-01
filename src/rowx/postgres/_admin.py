from dataclasses import dataclass
from typing import Final
from urllib.parse import quote, unquote, urlencode, urlparse, urlunparse

from rowx._core.postgres import (
    create_database as create_database_impl,
    database_exists as database_exists_impl,
    drop_database as drop_database_impl,
    list_databases as list_databases_impl,
)
from rowx.postgres._options import ConnectOptions

URL_SCHEMES: Final[tuple[str, ...]] = ("postgres://", "postgresql://")


# `dsn` (a URL or a key=value string) pointed at another database, as a string the driver reads back as `name`
def with_database(dsn: str, name: str) -> str:
    dsn = dsn.strip()
    if dsn.startswith(URL_SCHEMES):
        parts = urlparse(dsn)
        # the driver percent-decodes query keys and applies them in order after the path, the last one winning, so
        # every spelling of `dbname` (`%64bname` too) is dropped and the new name goes last; `quote` because the
        # driver doesn't read `+` as a space
        kept = [pair for pair in parts.query.split("&") if pair and unquote(pair.partition("=")[0]) != "dbname"]
        query = "&".join([*kept, urlencode({"dbname": name}, quote_via=quote)])
        url = urlunparse(parts._replace(path=f"/{quote(name, safe='')}", query=query))
    else:
        escaped = name.replace("\\", "\\\\").replace("'", "\\'")
        url = f"{dsn} dbname='{escaped}'"
    # a guard against any other way the URL could still select a different database
    if ConnectOptions.from_dsn(url).database != name:
        msg = "the database URL could not be pointed at another database"
        raise ValueError(msg)
    return url


# each connects to the maintenance database; `name` defaults to the database in `target`
async def database_exists(target: str | ConnectOptions, name: str | None = None) -> bool:
    return await database_exists_impl(target, name)


async def create_database(
    target: str | ConnectOptions,
    name: str | None = None,
    *,
    template: str | None = None,
    comment: str | None = None,
) -> None:
    await create_database_impl(target, name, template, comment)


# a missing database is not an error
async def drop_database(
    target: str | ConnectOptions,
    name: str | None = None,
    *,
    force: bool = False,
) -> None:
    await drop_database_impl(target, name, force)


@dataclass(slots=True, kw_only=True, frozen=True)
class DatabaseInfo:
    name: str
    comment: str | None
    sessions: int


# the databases whose name starts with `prefix`, by name; `sessions` counts the connections open right now
async def list_databases(target: str | ConnectOptions, prefix: str) -> list[DatabaseInfo]:
    rows = await list_databases_impl(target, prefix)
    return [DatabaseInfo(name=name, comment=comment, sessions=sessions) for name, comment, sessions in rows]

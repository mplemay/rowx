from typing import TYPE_CHECKING

from rowx._core.postgres import (
    create_database as create_database_impl,
    database_exists as database_exists_impl,
    drop_database as drop_database_impl,
)

if TYPE_CHECKING:
    from rowx.postgres._options import ConnectOptions


# each connects to the maintenance database; `name` defaults to the database in `target`
async def database_exists(target: str | ConnectOptions, name: str | None = None) -> bool:
    return await database_exists_impl(target, name)


async def create_database(
    target: str | ConnectOptions,
    name: str | None = None,
    *,
    template: str | None = None,
) -> None:
    await create_database_impl(target, name, template)


# a missing database is not an error
async def drop_database(
    target: str | ConnectOptions,
    name: str | None = None,
    *,
    force: bool = False,
) -> None:
    await drop_database_impl(target, name, force)

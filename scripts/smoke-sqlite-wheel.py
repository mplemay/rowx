#!/usr/bin/env python3
from asyncio import run
from importlib import import_module
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final
from urllib.parse import quote

import rowx_sqlite
from rowx_sqlite import ConnectOptions, Migrator, connect

import rowx.sqlite
from rowx import query

# the compiled extension and the typing files ship, and a file database survives a close and reopen; the directory name
# holds a space so a Windows path with a drive letter and a space goes through both the options and the DSN
NAME: Final[str] = "rowx smoke"
MIGRATION: Final[str] = "CREATE TABLE migrated (id INTEGER PRIMARY KEY);"
EXPECTED_ITEMS: Final[list[tuple[int, str]]] = [(1, "first")]


def require(condition: object, message: str) -> None:
    if not condition:
        raise SystemExit(message)


async def exercise(directory: Path) -> None:
    database = directory / "app.db"
    migrations = directory / "migrations"
    migrations.mkdir()
    (migrations / "1_migrated.sql").write_text(MIGRATION, encoding="utf-8")

    async with await connect(ConnectOptions(path=str(database), mode="rwc")) as conn:
        await query(t"CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT NOT NULL)").execute(conn)
        await query(t"INSERT INTO items (id, name) VALUES (1, {'first'})").execute(conn)
        items = await query[tuple[int, str]](t"SELECT id, name FROM items").all(conn)
        require(items == EXPECTED_ITEMS, f"the first read returned {items!r}")
        applied = await Migrator(migrations).run(conn)
        require(len(applied) == 1, f"the migrator applied {len(applied)} migrations, expected 1")

    # the DSN grammar: the path follows `sqlite:///` and is percent-decoded, so quote() turns a drive letter path into
    # `sqlite:///C%3A/...` and a POSIX path into `sqlite:////tmp/...`
    dsn = "sqlite:///" + quote(database.as_posix())
    async with await connect(dsn) as conn:
        items = await query[tuple[int, str]](t"SELECT id, name FROM items").all(conn)
        require(items == EXPECTED_ITEMS, f"the read after reopening returned {items!r}")
        tables = await query[str](t"SELECT name FROM sqlite_master WHERE name = 'migrated'").all(conn)
        require(tables == ["migrated"], f"the migrated table is missing after reopening: {tables!r}")


def main() -> int:
    require(rowx.sqlite.Connection is rowx_sqlite.Connection, "rowx.sqlite does not re-export rowx_sqlite")
    require(import_module("rowx_sqlite._core") is not None, "rowx_sqlite._core did not import")
    package = files("rowx_sqlite")
    for marker in ("_core.pyi", "py.typed"):
        require(package.joinpath(marker).is_file(), f"rowx_sqlite does not ship {marker}")
    with TemporaryDirectory(prefix=f"{NAME} ") as directory:
        run(exercise(Path(directory)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

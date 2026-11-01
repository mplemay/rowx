import asyncio
import os
import sys
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Final

from rowx import query
from rowx.postgres import Migrator, Pool

if TYPE_CHECKING:
    from rowx import Executor

MIGRATIONS: Final[Path] = Path(__file__).resolve().parent / "migrations"

type AuthorRow = tuple[int, str, str | None]
type BookRow = tuple[int, str, date | None]


async def add_author(db: Executor, name: str, bio: str | None = None) -> int:
    return await query[int](
        t"""
        INSERT INTO authors (name, bio) VALUES ({name}, {bio})
        ON CONFLICT (name) DO UPDATE SET bio = excluded.bio
        RETURNING id
        """,
    ).one(db)


async def add_book(db: Executor, author_id: int, title: str, published: date | None = None) -> int:
    return await query[int](
        t"INSERT INTO books (author_id, title, published) VALUES ({author_id}, {title}, {published}) RETURNING id"
    ).one(db)


async def list_authors(db: Executor) -> list[AuthorRow]:
    return await query[AuthorRow](t"SELECT id, name, bio FROM authors ORDER BY name").all(db)


async def books_by(db: Executor, author_id: int) -> list[BookRow]:
    return await query[BookRow](
        t"SELECT id, title, published FROM books WHERE author_id = {author_id} ORDER BY title"
    ).all(db)


async def main(dsn: str) -> None:
    # reads and validates the files now, with no database IO
    migrator = Migrator(MIGRATIONS)
    async with Pool(dsn, max_size=4) as pool:
        # taking the advisory lock makes this safe when several instances start at once
        for status in await migrator.run(pool):
            print(f"applied {status.version} {status.description} in {status.execution_time}")  # ruff: ignore[print]

        author_id = await add_author(pool, "Octavia Butler")
        if not await books_by(pool, author_id):
            await add_book(pool, author_id, "Kindred", date(1979, 6, 1))
        for _, name, _ in await list_authors(pool):
            print(f"{name}: {len(await books_by(pool, author_id))} book(s)")  # ruff: ignore[print]


if __name__ == "__main__":
    if not (url := os.environ.get("DATABASE_URL")):
        sys.exit("DATABASE_URL is not set")
    asyncio.run(main(url))

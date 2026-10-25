import asyncio
import os
import sys
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from rowx import query
from rowx.postgres import connect

if TYPE_CHECKING:
    from rowx.postgres import Connection

type AuthorRow = tuple[int, str, str | None]
type BookRow = tuple[int, str, Decimal, date | None, list[str]]


async def list_authors(conn: Connection) -> list[AuthorRow]:
    return await query[AuthorRow](t"SELECT id, name, bio FROM authors ORDER BY name").all(conn)


async def books_by(conn: Connection, author: str) -> list[BookRow]:
    return await query[BookRow](
        t"""
        SELECT b.id, b.title, b.price, b.published, b.tags
        FROM books b
        JOIN authors a ON a.id = b.author_id
        WHERE a.name = {author}
        ORDER BY b.title
        """,
    ).all(conn)


async def books_tagged(conn: Connection, tags: list[str]) -> list[str]:
    return await query[str](t"SELECT title FROM books WHERE tags && {tags} ORDER BY title").all(conn)


async def book_counts(conn: Connection) -> list[tuple[str, int, Decimal | None]]:
    return await query[tuple[str, int, Decimal | None]](
        t"""
        SELECT a.name, count(b.id) AS "books!", avg(b.price)
        FROM authors a
        LEFT JOIN books b ON b.author_id = a.id
        GROUP BY a.name
        ORDER BY a.name
        """,
    ).all(conn)


async def add_book(conn: Connection, author: str, title: str, price: Decimal) -> int:
    return await query[int](
        t"""
        INSERT INTO books (author_id, title, price)
        SELECT id, {title}, {price} FROM authors WHERE name = {author}
        RETURNING id
        """,
    ).one(conn)


async def main(dsn: str) -> None:
    async with await connect(dsn) as conn:
        for author_id, name, bio in await list_authors(conn):
            print(f"{author_id}. {name}: {bio or 'no bio'}")  # ruff: ignore[print]

        for _, title, price, published, tags in await books_by(conn, "Ursula Le Guin"):
            print(f"{title} ({published or 'unpublished'}) ${price} [{', '.join(tags)}]")  # ruff: ignore[print]

        print("tagged:", ", ".join(await books_tagged(conn, ["history", "fantasy"])))  # ruff: ignore[print]

        for name, count, average in await book_counts(conn):
            print(f"{name}: {count} books, average price {average}")  # ruff: ignore[print]

        # roll back so the example can run repeatedly against the same database
        await query(t"BEGIN").execute(conn)
        try:
            await add_book(conn, "Octavia Butler", "Dawn", Decimal("10.00"))
            print(f"inside the transaction: {len(await books_by(conn, 'Octavia Butler'))} books")  # ruff: ignore[print]
        finally:
            await query(t"ROLLBACK").execute(conn)
        print(f"after the rollback: {len(await books_by(conn, 'Octavia Butler'))} books")  # ruff: ignore[print]


if __name__ == "__main__":
    if not (url := os.environ.get("DATABASE_URL")):
        sys.exit("DATABASE_URL is not set")
    asyncio.run(main(url))

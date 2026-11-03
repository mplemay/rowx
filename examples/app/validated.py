import asyncio
import os
import sys
from datetime import date  # ruff: ignore[typing-only-standard-library-import]
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, Field

from rowx import query
from rowx.postgres import connect

if TYPE_CHECKING:
    from rowx.postgres import Connection


# a pydantic model is validated as `Book.model_validate` would; it lives apart from main.py so that
# main.py runs with rowx alone (pydantic is only needed for this module)
class Book(BaseModel):
    id: int
    title: str
    price: Annotated[Decimal, Field(ge=0)]
    published: date | None
    tags: list[str]


async def cheap_books(conn: Connection, limit: Decimal) -> list[Book]:
    return await query[Book](
        t"SELECT id, title, price, published, tags FROM books WHERE price < {limit} ORDER BY title",
    ).all(conn)


async def main(dsn: str) -> None:
    async with await connect(dsn) as conn:
        for book in await cheap_books(conn, Decimal(12)):
            print(f"cheap: {book.title} ${book.price}")  # ruff: ignore[print]


if __name__ == "__main__":
    if not (url := os.environ.get("DATABASE_URL")):
        sys.exit("DATABASE_URL is not set")
    asyncio.run(main(url))

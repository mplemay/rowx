import asyncio
from datetime import date
from typing import TYPE_CHECKING

from bookshelf import add_author, add_book, books_by, list_authors

from rowx import query

if TYPE_CHECKING:
    from rowx.postgres import Pool

# The fixtures are synchronous, so these tests drive the async code with `asyncio.run`. With pytest-asyncio (or anyio
# on its asyncio backend) the tests can be `async def` instead.


def test_each_test_starts_from_the_migrated_schema(rowx_pool: Pool) -> None:
    async def work() -> int:
        return await query[int](t'SELECT count(*) AS "n!" FROM authors').one(rowx_pool)

    assert asyncio.run(work()) == 0  # ruff: ignore[assert]


def test_a_book_belongs_to_an_author(rowx_pool: Pool) -> None:
    async def work() -> None:
        author_id = await add_author(rowx_pool, "Octavia Butler", "Science fiction")
        await add_book(rowx_pool, author_id, "Kindred", date(1979, 6, 1))
        assert await list_authors(rowx_pool) == [(author_id, "Octavia Butler", "Science fiction")]  # ruff: ignore[assert]
        assert [title for _, title, _ in await books_by(rowx_pool, author_id)] == ["Kindred"]  # ruff: ignore[assert]

    asyncio.run(work())


def test_a_second_test_does_not_see_the_first_ones_rows(rowx_pool: Pool) -> None:
    async def work() -> None:
        assert await list_authors(rowx_pool) == []  # ruff: ignore[assert]
        await add_author(rowx_pool, "Ursula Le Guin")

    asyncio.run(work())

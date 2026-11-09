import asyncio
import os
import sys
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, assert_never

from rowx_postgres import connect

from rowx import query, query_unchecked

if TYPE_CHECKING:
    from string.templatelib import Template

    from rowx_postgres import Connection

type ProductRow = tuple[int, str, str, Decimal, int]
type Direction = Literal["asc", "desc"]
type GroupColumn = Literal["category", "vendor"]


# the allowlist for a column a caller may sort by. `internal_cost` is a real column, but it is not a member, and
# sorting by a column reveals the order of its values even when it is never selected
class SortColumn(StrEnum):
    NAME = "name"
    PRICE = "price"
    STOCK = "stock"


def parse_sort(raw: str) -> SortColumn:
    # raises ValueError for anything that is not a member, before any SQL is built
    return SortColumn(raw)


def parse_direction(raw: str) -> Direction:
    match raw:
        case "asc" | "desc" as direction:
            return direction
        case _:
            msg = f"unknown sort direction: {raw!r}"
            raise ValueError(msg)


def direction_sql(direction: Direction) -> Template:
    match direction:
        case "asc":
            return t"ASC"
        case "desc":
            return t"DESC"
        case _:
            assert_never(direction)


def group_column(column: GroupColumn) -> Template:
    match column:
        case "category" | "vendor":
            return t"{column:ident}"
        case _:
            assert_never(column)


# the values are bound, and `query` is checked by `rowx check`
async def products_by_id(conn: Connection, ids: list[int]) -> list[ProductRow]:
    return await query[ProductRow](
        t"SELECT id, name, category, price, stock FROM products WHERE id = ANY({ids}) ORDER BY id",
    ).all(conn)


# an optional filter is one static statement: a NULL parameter turns its condition off
async def search(conn: Connection, category: str | None = None, max_price: Decimal | None = None) -> list[ProductRow]:
    return await query[ProductRow](
        t"""
        SELECT id, name, category, price, stock
        FROM products
        WHERE ({category}::text IS NULL OR category = {category})
          AND ({max_price}::numeric IS NULL OR price <= {max_price})
        ORDER BY id
        """,
    ).all(conn)


# an identifier and a direction cannot be parameters, so the statement is unchecked, and both come from an allowlist
async def listing(
    conn: Connection, *, sort: SortColumn, direction: Direction = "asc", category: str | None = None
) -> list[ProductRow]:
    return await query_unchecked[ProductRow](
        t"""
        SELECT id, name, category, price, stock
        FROM products
        WHERE ({category}::text IS NULL OR category = {category})
        ORDER BY {sort:ident} {direction_sql(direction)}, id
        """,
    ).all(conn)


async def stock_by(conn: Connection, column: GroupColumn) -> list[tuple[str | None, int]]:
    grouped = group_column(column)
    return await query_unchecked[tuple[str | None, int]](
        t'SELECT {grouped}, sum(stock)::int4 AS "total!" FROM products GROUP BY 1 ORDER BY 1 NULLS LAST',
    ).all(conn)


async def main(dsn: str) -> None:
    async with await connect(dsn) as conn:
        for _, name, _, price, stock in await listing(conn, sort=parse_sort("price"), direction="desc"):
            print(f"{name}: ${price}, {stock} in stock")  # ruff: ignore[print]

        for key, total in await stock_by(conn, "category"):
            print(f"{key}: {total}")  # ruff: ignore[print]

        try:
            parse_sort("internal_cost")
        except ValueError:
            print("internal_cost is not a sort column")  # ruff: ignore[print]


if __name__ == "__main__":
    if not (url := os.environ.get("DATABASE_URL")):
        sys.exit("DATABASE_URL is not set")
    asyncio.run(main(url))

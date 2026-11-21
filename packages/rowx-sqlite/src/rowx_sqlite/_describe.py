from typing import TYPE_CHECKING

from rowx_sqlite._compiler import statement_facts
from rowx_sqlite._core import describe as describe_impl

if TYPE_CHECKING:
    from rowx_sqlite._core import Connection, Describe


async def describe(conn: Connection, sql: str) -> Describe:
    facts = statement_facts(sql)
    return await describe_impl(
        conn,
        sql,
        facts.sources,
        (bool(facts.cte_names), facts.has_subquery, facts.has_compound, facts.has_outer_join),
    )

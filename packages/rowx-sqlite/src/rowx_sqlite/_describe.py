from typing import TYPE_CHECKING

from rowx_sqlite._compiler import statement_facts
from rowx_sqlite._core import describe as describe_impl

if TYPE_CHECKING:
    from rowx_sqlite._core import Connection, ConstructName, Describe


async def describe(conn: Connection, sql: str) -> Describe:
    facts = statement_facts(sql)
    found = (
        ("cte", bool(facts.cte_names)),
        ("subquery", facts.has_subquery),
        ("compound", facts.has_compound),
        ("outer_join", facts.has_outer_join),
        ("aggregate", facts.has_aggregate),
    )
    constructs: list[ConstructName] = [name for name, present in found if present]
    return await describe_impl(
        conn,
        sql,
        facts.sources,
        constructs=constructs,
    )

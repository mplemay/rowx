from rowx import query

value = 1
label = "a"


def clean() -> None:
    query[int](t'SELECT 1 AS "n!"')
    query[tuple[str, int | None]](t'SELECT \'a\'::text AS "s!", NULL::int AS "n?"')
    query[int](t'SELECT {value}::int AS "n!"')
    query[str](t'SELECT {label}::text AS "s!"')
    query(t"SELECT pg_sleep(0)")
    query[None](t"SELECT pg_sleep(0)")

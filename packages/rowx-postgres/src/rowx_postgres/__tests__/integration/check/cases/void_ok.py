from rowx import query


def run() -> None:
    query(t"SELECT pg_sleep(0)")
    query[None](t"SELECT pg_sleep(0)")
    query(t"UPDATE users SET name = name")
    query[tuple[int, None]](t'SELECT 1 AS "n!", pg_sleep(0)')

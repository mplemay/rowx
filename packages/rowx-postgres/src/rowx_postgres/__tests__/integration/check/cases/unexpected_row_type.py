from rowx import query


def run() -> None:
    query[int](t"UPDATE users SET name = name")  # error: [unexpected-row-type]
    query[int](t"SELECT pg_sleep(0)")  # error: [unexpected-row-type]
    query[None](t"UPDATE users SET name = name")

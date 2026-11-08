from rowx import query


def run() -> None:
    query[int](t"SELEC 1")  # error: [sql-error]
    query[int](t"SELECT id FROM usrs")  # error: [sql-error]
    query[int](t"SELECT id FROM users WHERE nope = 1")  # error: [sql-error]

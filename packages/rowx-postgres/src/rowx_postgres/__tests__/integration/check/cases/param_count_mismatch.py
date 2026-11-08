from rowx import query

q = "x"


def run() -> None:
    query[int](t"SELECT id FROM users WHERE name LIKE '%{q}%'")  # error: [param-count-mismatch]
    query[int](t"SELECT id FROM users WHERE name = {q}")
    query[int](t"SELECT id FROM users WHERE name = {q} -- {q}")  # error: [param-count-mismatch]

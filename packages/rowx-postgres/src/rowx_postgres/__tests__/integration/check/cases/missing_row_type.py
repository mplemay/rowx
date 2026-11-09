from rowx import query


def run() -> None:
    query(t"SELECT id FROM users")  # error: [missing-row-type]
    query(t"SELECT id, name FROM users")  # error: [missing-row-type]

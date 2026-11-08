from rowx import query


def run() -> None:
    query[tuple[int, str]](t"SELECT id FROM users")  # error: [arity-mismatch]
    query[int](t"SELECT id, name FROM users")  # error: [arity-mismatch]
    query[tuple[int, str, str]](t"SELECT id, name FROM users")  # error: [arity-mismatch]

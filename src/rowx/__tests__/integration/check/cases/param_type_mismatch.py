from rowx import query


def run() -> None:
    query[int](t'SELECT {"a"}::int AS "n!"')  # error: [param-type-mismatch]
    query[str](t'SELECT {42}::text AS "s!"')  # error: [param-type-mismatch]
    query[int](t'SELECT {True}::int AS "n!"')  # error: [param-type-mismatch]
    query[int](t'SELECT {1}::int + {"b"}::int AS "n!"')  # error: [param-type-mismatch]
    query[int](t'SELECT {[1, 2]}::int AS "n!"')  # error: [param-type-mismatch]
    query[int](t'SELECT count(*)::int AS "n!" WHERE 1 = ANY({["a", "b"]}::int[])')  # error: [param-type-mismatch]

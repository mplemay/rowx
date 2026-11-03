from rowx import query, query_unchecked

name = "users"
spec = ">5"


def run() -> None:
    query[int](f"SELECT id FROM {name}")  # error: [dynamic-query]  # ruff: ignore[hardcoded-sql-expression]  # ty: ignore[invalid-argument-type]
    query[int](t"SELECT id FROM users WHERE id {spec!r}")  # error: [dynamic-query]
    query[int](t"SELECT id FROM users WHERE id {spec:>5}")  # error: [dynamic-query]
    query[int](t"SELECT id FROM users LIMIT {t'{spec}'}")  # error: [dynamic-query]
    query[int](t"SELECT id FROM users", t"SELECT 1")  # error: [dynamic-query]  # ty: ignore[too-many-positional-arguments]
    query_unchecked(t"SELECT 1 FROM {name:ident}")
    query_unchecked(t"SELECT 1 FROM {name:{spec}}")  # error: [dynamic-query]
    query_unchecked(t"SELECT {1:>5} FROM {name:ident}")

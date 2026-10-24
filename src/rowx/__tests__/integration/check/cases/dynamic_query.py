from rowx import query

name = "users"
spec = ">5"


def run() -> None:
    query[int](f"SELECT id FROM {name}")  # error: [dynamic-query]  # noqa: S608  # ty: ignore[invalid-argument-type]
    query[int](t"SELECT id FROM users WHERE id {spec!r}")  # error: [dynamic-query]
    query[int](t"SELECT id FROM users WHERE id {spec:>5}")  # error: [dynamic-query]
    query[int](t"SELECT id FROM users LIMIT {t'{spec}'}")  # error: [dynamic-query]
    query[int](t"SELECT id FROM users", t"SELECT 1")  # error: [dynamic-query]  # ty: ignore[too-many-positional-arguments]

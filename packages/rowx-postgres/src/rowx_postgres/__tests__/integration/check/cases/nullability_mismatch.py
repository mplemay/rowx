from rowx import query


def run() -> None:
    query[tuple[int, str, str]](t"SELECT id, name, email FROM users")  # error: [nullability-mismatch]
    query[tuple[int, str, str | None]](t"SELECT id, name, email FROM users")
    query[tuple[int, str | None]](t"SELECT id, name FROM users")  # error: [nullability-mismatch]
    query[str](t'SELECT email AS "email!" FROM users WHERE email IS NOT NULL')
    query[str | None](t"SELECT email FROM users")
    query[int](t"SELECT length(name) FROM users")  # error: [nullability-mismatch]
    query[int](t'SELECT 1 AS "n!"')
    query[int](t"SELECT p.id FROM users u LEFT JOIN posts p ON p.user_id = u.id")  # error: [nullability-mismatch]
    query[int](t"SELECT x FROM not_valid")  # error: [nullability-mismatch]

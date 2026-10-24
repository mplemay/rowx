from rowx import query


def run() -> None:
    query[str](t'SELECT current_user::text AS "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa!"')  # error: [invalid-override, nullability-mismatch]  # fmt: skip

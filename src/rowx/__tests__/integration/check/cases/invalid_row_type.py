from rowx import query


def run() -> None:
    query[dict](t"SELECT 1")  # error: [invalid-row-type]  # ty: ignore[missing-type-argument]
    query[tuple[()]](t"SELECT 1")  # error: [invalid-row-type]

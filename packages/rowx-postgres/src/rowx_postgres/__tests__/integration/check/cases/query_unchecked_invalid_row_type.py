from rowx import query_unchecked


def run() -> None:
    query_unchecked[dict](t"SELECT this is not sql")  # error: [invalid-row-type]  # ty: ignore[missing-type-argument]

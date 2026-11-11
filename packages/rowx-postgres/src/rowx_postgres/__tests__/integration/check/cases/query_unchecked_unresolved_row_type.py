from rowx import query_unchecked

Row = tuple[int]
Row = tuple[str]


def run() -> None:
    query_unchecked[Row](t"SELECT 1")  # error: [unresolved-row-type]

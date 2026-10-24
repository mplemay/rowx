from rowx import query

Row = tuple[int]
Row = tuple[str]


def run() -> None:
    query[Row](t"SELECT 1")  # error: [unresolved-row-type]

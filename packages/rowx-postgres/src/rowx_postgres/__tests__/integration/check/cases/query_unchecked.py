from rowx import query, query_unchecked


def run() -> None:
    query_unchecked(t"SELECT whatever FROM nowhere")
    query_unchecked(t"SELEC 1")
    query[int](t"SELECT id FROM users LIMIT 1")

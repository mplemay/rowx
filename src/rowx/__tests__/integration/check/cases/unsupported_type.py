from rowx import query


def run() -> None:
    query[str](t'SELECT c_money::text AS "m!" FROM unsupported_types WHERE c_money IS NOT NULL')
    query[str](t"SELECT c_money FROM unsupported_types")  # error: [unsupported-type]
    query[str](t"SELECT c_tsvector FROM unsupported_types")  # error: [unsupported-type]
    query[str](t"SELECT c_int4range FROM unsupported_types")  # error: [unsupported-type]

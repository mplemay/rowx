from rowx import query


def run() -> None:
    query[str](t'SELECT c_xml::text AS "m!" FROM unsupported_types WHERE c_xml IS NOT NULL')
    query[str](t"SELECT c_xml FROM unsupported_types")  # error: [unsupported-type]
    query[str](t"SELECT c_tsvector FROM unsupported_types")  # error: [unsupported-type]
    query[str](t"SELECT c_int4multirange FROM unsupported_types")  # error: [unsupported-type]
    query[str](t"SELECT c_floatrange FROM unsupported_types")  # error: [unsupported-type]

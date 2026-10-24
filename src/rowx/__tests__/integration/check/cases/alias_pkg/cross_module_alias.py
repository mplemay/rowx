from alias_pkg import rows
from alias_pkg.rows import OkRow, Row
from rowx import query


def run() -> None:
    query[Row](t"SELECT id, name, email FROM users")  # error: [nullability-mismatch]
    query[OkRow](t"SELECT id, name, email FROM users")
    query[rows.Row](t"SELECT id, name, email FROM users")  # error: [nullability-mismatch]

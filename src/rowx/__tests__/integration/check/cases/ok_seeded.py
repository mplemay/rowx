from ipaddress import IPv4Interface, IPv4Network, IPv6Interface, IPv6Network

from rowx import query


def run() -> None:
    query[tuple[int, str, str | None]](t"SELECT id, name, email FROM users")
    query[tuple[int, str]](t"SELECT id, name FROM users")
    query[tuple[int, str | None]](t"SELECT u.id, p.title FROM users u LEFT JOIN posts p ON p.user_id = u.id")
    query[tuple[str, str | None]](t"SELECT u.name, o.note FROM users u JOIN orders o ON o.user_id = u.id")
    query[tuple[str, int]](t'SELECT name, count(*) AS "n!" FROM users GROUP BY name')
    query[tuple[int, int | None, bool | None]](t"SELECT id, c_int4, c_bool FROM all_types")
    query[tuple[int, str | None, list[int | None] | None]](t"SELECT id, c_text, c_int4_array FROM all_types")
    query[tuple[int | None, str | None]](t"SELECT user_id, title FROM user_posts")
    query[tuple[int, int | None]](t"SELECT id, x FROM not_valid")
    query[tuple[str, int]](t'SELECT coalesce(email, \'none\') AS "e!", 1 AS "n!" FROM users')
    query[str](t'SELECT c_text AS "t!" FROM all_types WHERE c_text IS NOT NULL')
    query(t"SELECT pg_sleep(0)")
    query[tuple[IPv4Interface | IPv6Interface | None, IPv4Network | IPv6Network | None, str | None, str | None]](
        t"SELECT c_inet, c_cidr, c_macaddr, c_macaddr8 FROM all_types"
    )

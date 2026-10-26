import ipaddress

from rowx import query
from rowx.postgres import Range


def run() -> None:
    query[dict](t"SELECT 1")  # error: [invalid-row-type]  # ty: ignore[missing-type-argument]
    query[tuple[()]](t"SELECT 1")  # error: [invalid-row-type]
    query[ipaddress.IPv4Interface](t"SELECT 1")  # error: [invalid-row-type]
    query[ipaddress.IPv4Interface | ipaddress.IPv6Network](t"SELECT 1")  # error: [invalid-row-type]
    query[ipaddress.IPv4Interface | ipaddress.IPv6Interface | int](t"SELECT 1")  # error: [invalid-row-type]
    query[Range](t"SELECT 1")  # error: [invalid-row-type]  # ty: ignore[missing-type-argument]
    query[Range[str]](t"SELECT 1")  # error: [invalid-row-type]
    query[Range[float]](t"SELECT 1")  # error: [invalid-row-type]
    query[Range[int | None]](t"SELECT 1")  # error: [invalid-row-type]

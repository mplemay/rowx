import asyncio
import os
from pathlib import Path
from typing import Final
from urllib.parse import urlparse

import pytest

from rowx.errors import InterfaceError
from rowx.postgres import Connection, connect

pytestmark = [pytest.mark.integration, pytest.mark.db]

CERTS: Final[Path] = Path(os.environ.get("ROWX_TEST_CERTS", Path(__file__).resolve().parents[5] / ".rowx-certs"))
GONE_WITHIN: Final[float] = 2.0
SSL_IN_USE: Final[str] = "SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()"
SLEEPING: Final[str] = (
    "SELECT count(*)::int4 FROM pg_stat_activity "
    "WHERE state = 'active' AND query LIKE '%pg_sleep(3)%' AND pid <> pg_backend_pid()"
)


def _require_env(name: str) -> str:
    if (value := os.environ.get(name)) is not None:
        return value
    if os.environ.get("ROWX_REQUIRE_DB") == "1":
        pytest.fail(f"ROWX_REQUIRE_DB=1 but {name} is not set")
    pytest.skip(f"{name} is not set")


@pytest.fixture
def ssl_dsn() -> str:
    return _require_env("ROWX_TEST_DSN_SSL")


def _kv(dsn: str, *, host: str | None = None, **extra: str) -> str:
    parts = urlparse(dsn)
    pairs = {
        "host": host or parts.hostname or "127.0.0.1",
        "port": str(parts.port or 5432),
        "user": parts.username or "",
        "password": parts.password or "",
        "dbname": parts.path.lstrip("/"),
        **extra,
    }
    if host is not None:
        pairs["hostaddr"] = parts.hostname or "127.0.0.1"
    return " ".join(f"{key}='{value}'" for key, value in pairs.items())


async def _fetch(conn: Connection, sql: str, *, scalar: bool = False) -> list[object]:
    mask = [False] if scalar else None
    return await conn._impl._fetch(sql, [], mask, scalar, scalar, 1 if scalar else None)


async def _scalar(conn: Connection, sql: str) -> object:
    (value,) = await _fetch(conn, sql, scalar=True)
    return value


async def _connect_and_check(dsn: str) -> object:
    conn = await connect(dsn)
    try:
        return await _scalar(conn, SSL_IN_USE)
    finally:
        await conn.close()


def _connect_error(dsn: str) -> None:
    with pytest.raises(InterfaceError):
        asyncio.run(_connect_and_check(dsn))


def test_verify_full_with_the_right_ca_succeeds(ssl_dsn: str) -> None:
    dsn = _kv(ssl_dsn, host="localhost", sslmode="verify-full", sslrootcert=str(CERTS / "ca.crt"))
    assert asyncio.run(_connect_and_check(dsn)) is True


def test_verify_full_with_the_wrong_ca_fails(ssl_dsn: str) -> None:
    dsn = _kv(ssl_dsn, host="localhost", sslmode="verify-full", sslrootcert=str(CERTS / "other-ca.crt"))
    _connect_error(dsn)


def test_verify_full_against_the_wrong_host_fails(ssl_dsn: str) -> None:
    dsn = _kv(ssl_dsn, host="wrong-host.example", sslmode="verify-full", sslrootcert=str(CERTS / "ca.crt"))
    _connect_error(dsn)


def test_verify_ca_against_the_wrong_host_succeeds(ssl_dsn: str) -> None:
    dsn = _kv(ssl_dsn, host="wrong-host.example", sslmode="verify-ca", sslrootcert=str(CERTS / "ca.crt"))
    assert asyncio.run(_connect_and_check(dsn)) is True


def test_verify_ca_with_the_wrong_ca_fails(ssl_dsn: str) -> None:
    dsn = _kv(ssl_dsn, sslmode="verify-ca", sslrootcert=str(CERTS / "other-ca.crt"))
    _connect_error(dsn)


def test_require_succeeds_without_a_ca(ssl_dsn: str) -> None:
    assert asyncio.run(_connect_and_check(_kv(ssl_dsn, sslmode="require"))) is True


def test_require_with_a_ca_verifies_the_chain(ssl_dsn: str) -> None:
    _connect_error(_kv(ssl_dsn, sslmode="require", sslrootcert=str(CERTS / "other-ca.crt")))


def test_prefer_encrypts_when_the_server_supports_it(ssl_dsn: str) -> None:
    assert asyncio.run(_connect_and_check(ssl_dsn)) is True


def test_prefer_against_a_non_ssl_server_falls_back_to_plaintext() -> None:
    dsn = _require_env("ROWX_TEST_DSN")

    async def run() -> object:
        conn = await connect(dsn)
        try:
            if await _scalar(conn, "SELECT current_setting('ssl')::bool"):
                pytest.skip("ROWX_TEST_DSN points at an SSL server")
            return await _scalar(conn, SSL_IN_USE)
        finally:
            await conn.close()

    assert asyncio.run(run()) is False


def test_disable_is_plaintext(ssl_dsn: str) -> None:
    assert asyncio.run(_connect_and_check(_kv(ssl_dsn, sslmode="disable"))) is False


@pytest.mark.parametrize("key", ["sslcert", "sslkey", "sslcrl"])
def test_client_certificate_settings_raise_interface_error(ssl_dsn: str, key: str) -> None:
    _connect_error(_kv(ssl_dsn, **{key: "/nonexistent"}))


def test_unreadable_sslrootcert_raises_interface_error(ssl_dsn: str) -> None:
    _connect_error(_kv(ssl_dsn, sslmode="verify-full", sslrootcert="/nonexistent/ca.pem"))


@pytest.mark.timing
def test_cancellation_works_over_tls(ssl_dsn: str) -> None:
    dsn = _kv(ssl_dsn, host="localhost", sslmode="verify-full", sslrootcert=str(CERTS / "ca.crt"))

    async def sleeping(observer: Connection) -> int:
        count = await _scalar(observer, SLEEPING)
        assert isinstance(count, int)
        return count

    async def wait_for_sleep(observer: Connection, *, present: bool) -> bool:
        # each poll is a round trip on the observer, so no sleep is needed
        loop = asyncio.get_running_loop()
        deadline = loop.time() + GONE_WITHIN
        while loop.time() < deadline:
            if bool(await sleeping(observer)) == present:
                return True
        return False

    async def sleep_three(conn: Connection) -> list[object]:
        return await _fetch(conn, "SELECT pg_sleep(3)")

    async def run() -> object:
        conn = await connect(dsn)
        observer = await connect(dsn)
        try:
            slow = asyncio.create_task(sleep_three(conn))
            assert await wait_for_sleep(observer, present=True)
            slow.cancel()
            with pytest.raises(asyncio.CancelledError):
                await slow
            assert await wait_for_sleep(observer, present=False)
            return await _scalar(conn, "SELECT 1")
        finally:
            await conn.close()
            await observer.close()

    assert asyncio.run(run()) == 1

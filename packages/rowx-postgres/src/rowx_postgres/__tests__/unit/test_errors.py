import asyncio

import pytest
from rowx_core.errors import InterfaceError
from rowx_postgres import ConnectOptions, connect


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://u:Zq9p%40ss@h:notaport/db",
        "host=h port=zz password=Zq9p@ss",
        "host=h password='Zq9p@ss",
        "postgresql://u:Zq9p%40ss@[unparseable/db",
        "postgres://u@h/db?%70assword=Zq9pw&sslmode=Zq9pw",
        "host=h password=Zq9\\ pw sslmode='Zq9 pw'",
        "host=h password=Zq9&pw sslmode=Zq9&pw",
    ],
)
def test_dsn_errors_never_carry_the_password(dsn: str) -> None:
    with pytest.raises(InterfaceError) as caught:
        ConnectOptions.from_dsn(dsn)
    for text in (str(caught.value), repr(caught.value), repr(caught.value.__cause__)):
        assert "Zq9p" not in text


def test_connect_path_errors_never_carry_the_password() -> None:
    options = ConnectOptions(host="h", password="Zq9pw", sslmode="bogus-Zq9pw")  # ruff: ignore[hardcoded-password-func-arg]
    with pytest.raises(InterfaceError) as caught:
        asyncio.run(connect(options))
    for text in (str(caught.value), repr(caught.value), repr(caught.value.__cause__)):
        assert "Zq9pw" not in text


def test_sslmode_and_sslrootcert_errors_name_the_setting_not_the_value() -> None:
    with pytest.raises(InterfaceError, match="unsupported sslmode") as caught:
        ConnectOptions.from_dsn("postgres://u@h/db?sslmode=Zq9pw")
    assert "Zq9pw" not in str(caught.value)
    options = ConnectOptions(host="h", sslmode="verify-ca", sslrootcert="/nonexistent/Zq9pw/ca.pem")
    with pytest.raises(InterfaceError, match="sslrootcert") as caught:
        asyncio.run(connect(options))
    assert "Zq9pw" not in str(caught.value)

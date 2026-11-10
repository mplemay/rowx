import asyncio
from typing import Any, Final, cast

import pytest
from rowx_core.errors import InterfaceError
from rowx_postgres import ConnectOptions, connect
from rowx_postgres._core import parse_dsn

PASSWORD: Final[str] = "hunter2-password"  # ruff: ignore[hardcoded-password-string]
EXTRA_SECRET: Final[str] = "hunter2-sslpassword"  # ruff: ignore[hardcoded-password-string]


def test_repr_never_shows_the_password_or_an_extra_value() -> None:
    opts = ConnectOptions(
        host="h",
        user="u",
        password=PASSWORD,
        extra={"sslpassword": EXTRA_SECRET, "keepalives": "0"},
    )
    shown = repr(opts)
    assert PASSWORD not in shown
    assert EXTRA_SECRET not in shown
    assert "'0'" not in shown


def test_repr_shows_extra_keys_redacted_and_server_settings() -> None:
    opts = ConnectOptions(
        host="h",
        port=5432,
        server_settings={"search_path": "app"},
        extra={"sslpassword": EXTRA_SECRET},
    )
    shown = repr(opts)
    assert "extra={'sslpassword': '<redacted>'}" in shown
    assert "server_settings={'search_path': 'app'}" in shown
    assert shown.startswith("ConnectOptions(host='h', port=5432, user=None, database=None")
    assert "password=" not in shown.replace("sslpassword", "")


def test_repr_of_a_parsed_dsn_hides_secrets() -> None:
    opts = ConnectOptions.from_dsn(f"postgresql://u:{PASSWORD}@h/db?sslpassword={EXTRA_SECRET}&keepalives=0")
    shown = repr(opts)
    assert PASSWORD not in shown
    assert EXTRA_SECRET not in shown
    assert "keepalives" in shown


def test_repr_never_raises_for_a_non_mapping_extra() -> None:
    opts = ConnectOptions(host="h", extra=cast("Any", None))
    assert "extra='<redacted>'" in repr(opts)


def test_unrelated_parse_errors_keep_their_text_after_another_dsn_was_parsed() -> None:
    parse_dsn("postgres://u:invalid@h/db")
    with pytest.raises(InterfaceError) as port:
        parse_dsn("host=h port=zz")
    assert "***" not in str(port.value)
    assert "port" in str(port.value)
    with pytest.raises(InterfaceError) as mode:
        parse_dsn("host=h sslmode=bogus")
    assert "***" not in str(mode.value)
    assert "sslmode" in str(mode.value)
    assert "bogus" not in str(mode.value)


def test_long_options_map_dashes_to_underscores() -> None:
    opts = ConnectOptions.from_dsn("host=h options='--search-path=x'")
    assert opts.server_settings == {"search_path": "x"}


@pytest.mark.parametrize(
    "options",
    [
        ConnectOptions(host="h", password="Zq9pw", sslmode="bogus-Zq9pw"),  # ruff: ignore[hardcoded-password-func-arg]
        ConnectOptions(
            host="h",
            password="Zq9pw",  # ruff: ignore[hardcoded-password-func-arg]
            sslmode="verify-full",
            sslrootcert="/nonexistent/Zq9pw/ca.pem",
        ),
        ConnectOptions(
            host="h",
            sslmode="verify-full",
            sslrootcert="/nonexistent/Zq9pw/ca.pem",
            extra={"sslpassword": "Zq9pw"},
        ),
    ],
)
def test_connect_path_errors_never_carry_the_password(options: ConnectOptions) -> None:
    with pytest.raises(InterfaceError) as caught:
        asyncio.run(connect(options))
    assert "Zq9pw" not in str(caught.value)
    assert "Zq9pw" not in repr(caught.value)


SECRET: Final[str] = "p@ss"  # ruff: ignore[hardcoded-password-string]
ENCODED: Final[str] = "p%40ss"
SECRET_FORMS: Final[list[str]] = [
    f"postgresql://u:{ENCODED}@h:notaport/db",
    f"postgresql://u@h:notaport/db?password={ENCODED}",
    f"postgresql://u@h/db?sslmode=disable&password={ENCODED}&port=zz",
    f"host=h port=zz password={SECRET}",
    f"host=h port=zz password='{SECRET}'",
    f"host=h password='{SECRET}",
    f"{SECRET} password={SECRET} oops",
    f"postgresql://u:{ENCODED}@[unparseable/db",
]


def assert_no_secret(exc: BaseException) -> None:
    for text in (str(exc), repr(exc), repr(exc.__cause__)):
        assert SECRET not in text
        assert ENCODED not in text


@pytest.mark.parametrize("dsn", SECRET_FORMS)
def test_dsn_errors_never_echo_the_password(dsn: str) -> None:
    with pytest.raises(InterfaceError) as caught:
        parse_dsn(dsn)
    assert_no_secret(caught.value)
    with pytest.raises(InterfaceError) as again:
        ConnectOptions.from_dsn(dsn)
    assert_no_secret(again.value)


@pytest.mark.parametrize(
    ("options", "key"),
    [
        (ConnectOptions(password="a\x00b"), "password"),  # ruff: ignore[hardcoded-password-func-arg]
        (ConnectOptions(extra={"sslpassword": "a\nb"}), "sslpassword"),
        (ConnectOptions(server_settings={"work_mem": "a\tb"}), "work_mem"),
        (ConnectOptions(server_settings={"a\tb": "1"}), "a\\tb"),
        (ConnectOptions(extra={"Bad": "1"}), "Bad"),
    ],
)
def test_invalid_options_name_the_key_and_never_the_value(options: ConnectOptions, key: str) -> None:
    with pytest.raises(InterfaceError) as caught:
        asyncio.run(connect(options))
    assert key in str(caught.value)
    assert "a\x00b" not in str(caught.value)


def test_dashed_extra_keys_are_still_rejected() -> None:
    with pytest.raises(InterfaceError):
        parse_dsn("postgres://u@h/db?bad-key=1")


def test_dash_in_a_c_option_name_maps_to_an_underscore() -> None:
    opts = ConnectOptions.from_dsn("host=h options='-c search-path=x'")
    assert opts.server_settings == {"search_path": "x"}


@pytest.mark.parametrize("value", ["disable", "prefer", "require"])
@pytest.mark.parametrize("form", ["postgresql://u@h/db?channel_binding={}", "host=h channel_binding={}"])
def test_channel_binding_is_parsed_into_the_typed_field(form: str, value: str) -> None:
    opts = ConnectOptions.from_dsn(form.format(value))
    assert opts.channel_binding == value
    assert "channel_binding" not in opts.extra


def test_channel_binding_defaults_to_none_and_shows_in_the_repr() -> None:
    assert ConnectOptions.from_dsn("host=h").channel_binding is None
    assert "channel_binding='require'" in repr(ConnectOptions(host="h", channel_binding="require"))
    assert "channel_binding=None" in repr(ConnectOptions(host="h"))


@pytest.mark.parametrize("dsn", ["host=h channel_binding=Zq9bogus", "postgresql://u@h/db?channel_binding=Zq9bogus"])
def test_an_invalid_channel_binding_in_a_dsn_names_the_key_not_the_value(dsn: str) -> None:
    with pytest.raises(InterfaceError, match="channel_binding") as caught:
        ConnectOptions.from_dsn(dsn)
    assert "Zq9" not in str(caught.value)


@pytest.mark.parametrize(
    "dsn",
    [
        "host=h sslmode=disable channel_binding=require",
        "postgresql://u@h/db?channel_binding=require&sslmode=disable",
    ],
)
def test_require_with_sslmode_disable_fails_at_parse_time_from_a_dsn(dsn: str) -> None:
    with pytest.raises(InterfaceError, match="channel_binding"):
        ConnectOptions.from_dsn(dsn)


def test_connect_rejects_the_field_forms_of_the_channel_binding_errors() -> None:
    cases = [
        ConnectOptions(host="h", sslmode="disable", channel_binding="require"),
        ConnectOptions(host="h", extra={"channel_binding": "require"}),
        ConnectOptions(host="h", channel_binding=cast("Any", "Zq9bogus")),
    ]
    for options in cases:
        with pytest.raises(InterfaceError, match="channel_binding") as caught:
            asyncio.run(connect(options))
        assert "Zq9" not in str(caught.value)


def test_with_connect_timeout_replaces_only_the_timeout() -> None:
    opts = ConnectOptions(host="h", database="db", connect_timeout=2)
    updated = opts.with_connect_timeout(0.25)
    assert updated is not opts
    assert updated.connect_timeout == pytest.approx(0.25)
    assert (updated.host, updated.database) == ("h", "db")
    assert opts.connect_timeout == pytest.approx(2)


def test_require_with_sslmode_disable_is_accepted_for_the_other_values() -> None:
    for value in ("disable", "prefer"):
        opts = ConnectOptions.from_dsn(f"host=h sslmode=disable channel_binding={value}")
        assert (opts.sslmode, opts.channel_binding) == ("disable", value)

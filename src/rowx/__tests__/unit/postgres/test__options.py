from rowx.postgres import ConnectOptions

PASSWORD = "hunter2-password"  # ruff: ignore[hardcoded-password-string]
EXTRA_SECRET = "hunter2-sslpassword"  # ruff: ignore[hardcoded-password-string]


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

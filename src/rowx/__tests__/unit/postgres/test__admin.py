import pytest

from rowx.postgres import ConnectOptions
from rowx.postgres._admin import with_database


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        ("postgresql://u:p@h:5432/app", "postgresql://u:p@h:5432/new?dbname=new"),
        ("postgres://u@h", "postgres://u@h/new?dbname=new"),
        ("postgresql://u@h/app?sslmode=disable&dbname=other", "postgresql://u@h/new?sslmode=disable&dbname=new"),
        ("postgresql://u@a:1,b:2/app?connect_timeout=3", "postgresql://u@a:1,b:2/new?connect_timeout=3&dbname=new"),
        ("host=h user=u dbname=app", "host=h user=u dbname=app dbname='new'"),
    ],
)
def test_with_database(dsn: str, expected: str) -> None:
    assert with_database(dsn, "new") == expected


# every spelling the driver reads as `dbname` must lose to the new name
@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://u@h/app?%64bname=production",
        "postgresql://u@h/app?%64%62%6E%61%6D%65=production&sslmode=disable",
        "postgresql://u@h/app?dbname=production&dbname=production",
        "postgresql://u@h?dbname=production",
        " postgresql://u@h/production ",
        "host=h dbname=production",
        "dbname='production' host=h",
    ],
)
def test_with_database_overrides_every_dbname(dsn: str) -> None:
    assert ConnectOptions.from_dsn(dsn).database == "production"
    url = with_database(dsn, "rowx_check_0123456789abcdef")
    assert ConnectOptions.from_dsn(url).database == "rowx_check_0123456789abcdef"


def test_with_database_keeps_everything_else() -> None:
    dsn = "postgresql://u:p%40ss@h:5433/app?sslmode=disable&application_name=a%20b&options=-c%20search_path%3Dx"
    before = ConnectOptions.from_dsn(dsn)
    after = ConnectOptions.from_dsn(with_database(dsn, "new"))
    assert after.database == "new"
    assert (after.host, after.port, after.user, after.password) == ("h", 5433, "u", "p@ss")
    assert (after.sslmode, after.application_name, after.server_settings) == (
        before.sslmode,
        before.application_name,
        before.server_settings,
    )


@pytest.mark.parametrize("name", ["odd name", "it's", "back\\slash", "ünï"])
def test_with_database_quotes_the_name(name: str) -> None:
    for dsn in ("postgresql://u@h/app", "host=h user=u"):
        assert ConnectOptions.from_dsn(with_database(dsn, name)).database == name

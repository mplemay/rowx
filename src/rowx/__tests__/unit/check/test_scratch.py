import pytest

from rowx.check.scratch import Scratch, with_database


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        ("postgresql://u:p@h:5432/app", "postgresql://u:p@h:5432/new"),
        ("postgres://u@h", "postgres://u@h/new"),
        ("postgresql://u@h/app?sslmode=disable&dbname=other", "postgresql://u@h/new?sslmode=disable"),
        ("postgresql://u@a:1,b:2/app?connect_timeout=3", "postgresql://u@a:1,b:2/new?connect_timeout=3"),
        ("host=h user=u dbname=app", "host=h user=u dbname=app dbname=new"),
    ],
)
def test_with_database(dsn: str, expected: str) -> None:
    assert with_database(dsn, "new") == expected


def test_scratch_names_fit_the_identifier_limit() -> None:
    names = {Scratch("postgresql://u@h/app").name for _ in range(20)}
    assert len(names) == 20
    for name in names:
        assert name.startswith("rowx_check_")
        assert len(name) == len("rowx_check_") + 16
        int(name.removeprefix("rowx_check_"), 16)

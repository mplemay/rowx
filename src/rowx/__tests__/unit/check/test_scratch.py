import pytest

from rowx.check.runner import CheckError
from rowx.check.scratch import Scratch


def test_scratch_names_fit_the_identifier_limit() -> None:
    names = {Scratch("postgresql://u@h/app").name for _ in range(20)}
    assert len(names) == 20
    for name in names:
        assert name.startswith("rowx_check_")
        assert len(name) == len("rowx_check_") + 16
        int(name.removeprefix("rowx_check_"), 16)


@pytest.mark.parametrize(
    "dsn",
    ["postgresql://u:hunter2secret@h/app?sslmode", "host=h password=hunter2secret dbname='unterminated"],
)
def test_scratch_url_that_cannot_be_parsed_never_quotes_the_dsn(dsn: str) -> None:
    with pytest.raises(CheckError) as caught:
        _ = Scratch(dsn).url
    assert str(caught.value) == "the database URL could not be parsed"

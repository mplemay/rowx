from typing import TYPE_CHECKING

import pytest
from rowx_core.backend import Backend, UnsupportedCapabilityError
from rowx_sqlite._backend import load

if TYPE_CHECKING:
    from collections.abc import Callable

DSN = "sqlite:///x.db"


def test_load_returns_an_empty_backend() -> None:
    assert load() == Backend()


@pytest.mark.parametrize(
    "capability",
    [
        lambda backend: backend.checker(),
        lambda backend: backend.admin(DSN),
        lambda backend: backend.migrations(DSN, "migrations"),
        lambda backend: backend.testing(DSN),
        lambda backend: backend.scratch(DSN),
    ],
    ids=["checker", "admin", "migrations", "testing", "scratch"],
)
def test_every_capability_is_unsupported(capability: Callable[[Backend], object]) -> None:
    with pytest.raises(UnsupportedCapabilityError):
        capability(load())


def test_optional_probes_report_nothing() -> None:
    backend = load()
    assert backend.embedded(DSN) is None
    assert backend.in_memory(DSN) is False

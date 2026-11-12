import os
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

if TYPE_CHECKING:
    from rowx_embedded.__tests__.support.fake_engine import FakeEngine

REQUIRE_ENV: Final[str] = "ROWX_REQUIRE_EMBEDDED"
TESTS_DIR: Final[Path] = Path(__file__).resolve().parent
# the real-engine tests of the engine binding run in their own CI step, not on the embedded lane
ENGINE_TESTS: Final[str] = "test__engine.py"


def _engine_loads() -> bool:
    try:
        package = import_module("rowx_embedded")
    except ImportError:
        return False
    # a wheel install has no `__tests__`; the tests (and their `support` helpers) are imported from the source tree
    if str(TESTS_DIR.parent) not in package.__path__:
        package.__path__.append(str(TESTS_DIR.parent))
    return True


# the tests need the engine (the root `just test-py` never builds it), so without it they are not collected, and with
# ROWX_REQUIRE_EMBEDDED=1 its absence is an error instead
collect_ignore_glob: list[str] = []
if not _engine_loads():
    collect_ignore_glob = ["unit/*", "integration/*", "support/*"]


def pytest_configure() -> None:
    if collect_ignore_glob and os.environ.get(REQUIRE_ENV) == "1":
        message = f"{REQUIRE_ENV}=1 but rowx_embedded could not be imported"
        raise pytest.UsageError(message)


# the embedded lane (`-m embedded`) selects these tests
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if TESTS_DIR in item.path.parents and item.path.name != ENGINE_TESTS:
            item.add_marker(pytest.mark.embedded)


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> FakeEngine:
    # imported here because the engine may be absent, in which case nothing is collected
    from rowx_embedded import _instance  # ruff: ignore[import-outside-top-level]
    from rowx_embedded.__tests__.support import fake_engine  # ruff: ignore[import-outside-top-level]

    fake = fake_engine.FakeEngine()
    monkeypatch.setattr(_instance, "Instance", fake)
    monkeypatch.setattr(_instance, "EngineError", fake_engine.FakeEngineError)
    monkeypatch.setattr(_instance, "SnapshotError", fake_engine.FakeSnapshotError)
    return fake

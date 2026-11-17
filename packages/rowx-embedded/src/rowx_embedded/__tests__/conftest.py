from pathlib import Path
from typing import Final

import pytest
from rowx_embedded import _instance
from rowx_embedded.__tests__.support import fake_engine

TESTS_DIR: Final[Path] = Path(__file__).resolve().parent
# the real-engine tests of the engine binding run on every lane, not only on the embedded one
ENGINE_TESTS: Final[str] = "test__engine.py"


# the embedded lane (`-m embedded`) selects these tests
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if TESTS_DIR in item.path.parents and item.path.name != ENGINE_TESTS:
            item.add_marker(pytest.mark.embedded)


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> fake_engine.FakeEngine:
    fake = fake_engine.FakeEngine()
    monkeypatch.setattr(_instance, "Instance", fake)
    monkeypatch.setattr(_instance, "EngineError", fake_engine.FakeEngineError)
    monkeypatch.setattr(_instance, "SnapshotError", fake_engine.FakeSnapshotError)
    return fake

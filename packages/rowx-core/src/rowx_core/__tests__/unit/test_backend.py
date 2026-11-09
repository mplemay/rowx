from typing import TYPE_CHECKING, override

import pytest
from rowx_core import Compiler
from rowx_core.backend import Backend, CheckerBackend, UnsupportedCapabilityError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from rowx_core import Compiled, TemplatePlan
    from rowx_core.backend import CheckerSession, ShapeIssue


class UnusedCompiler(Compiler):
    @override
    def compile(self, plan: TemplatePlan, /) -> tuple[Compiled, list[object]]:
        message = "constructing a backend must not compile queries"
        raise AssertionError(message)

    @override
    def compile_parts(self, strings: Sequence[str], /) -> Compiled:
        message = "constructing a backend must not compile queries"
        raise AssertionError(message)


async def unused_connect(_dsn: str, _timeout: float) -> CheckerSession:  # ruff: ignore[unused-async]
    message = "constructing a backend must not open a connection"
    raise AssertionError(message)


def unused_mapping(*_args: object) -> tuple[Sequence[int | None], Sequence[ShapeIssue]]:
    message = "constructing a backend must not map columns"
    raise AssertionError(message)


def test_backend_can_provide_only_query_checking() -> None:
    checker = CheckerBackend(
        connect_checker=unused_connect,
        compiler=UnusedCompiler(),
        map_columns=unused_mapping,
        types={},
        param_classes={},
        value_leaves={},
        range_leaves={},
        range_classes=frozenset(),
        value_classes={},
    )
    backend = Backend(checker_factory=lambda: checker)
    assert backend.checker() is checker
    with pytest.raises(UnsupportedCapabilityError, match="database administration"):
        backend.admin("fake://example")
    with pytest.raises(UnsupportedCapabilityError, match="migrations"):
        backend.migrations("fake://example", "migrations")
    with pytest.raises(UnsupportedCapabilityError, match="test database provisioning"):
        backend.testing("fake://example")

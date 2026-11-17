from typing import TYPE_CHECKING, cast, override

import pytest
from rowx_core import Compiler
from rowx_core.backend import (
    Backend,
    CheckerBackend,
    ScratchBuild,
    ScratchService,
    UnsupportedCapabilityError,
    dollar_placeholder,
    text_cast,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from rowx_core import Compiled, TemplatePlan
    from rowx_core.backend import CheckerSession, EmbeddedService, ShapeIssue


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


def test_a_backend_without_an_embedded_factory_has_no_embedded_urls() -> None:
    assert Backend().embedded("embedded://") is None


def test_the_embedded_factory_decides_which_urls_are_embedded() -> None:
    marker = object()
    seen: list[str] = []

    def factory(dsn: str) -> EmbeddedService | None:
        seen.append(dsn)
        return cast("EmbeddedService", marker) if dsn.startswith("embedded:") else None

    backend = Backend(embedded_factory=factory)
    assert backend.embedded("embedded://") is marker
    assert backend.embedded("postgresql://h/db") is None
    assert seen == ["embedded://", "postgresql://h/db"]


def test_a_scratch_build_holds_one_url_per_worker() -> None:
    build = ScratchBuild(urls=("a", "b"), seconds=1.5)
    assert (build.urls, build.seconds) == (("a", "b"), 1.5)


def test_a_backend_without_a_scratch_factory_has_no_scratch() -> None:
    with pytest.raises(UnsupportedCapabilityError, match="scratch databases"):
        Backend().scratch("fake://example")


def test_the_scratch_factory_builds_the_scratch() -> None:
    marker = object()
    seen: list[tuple[str, bool]] = []

    def factory(dsn: str, /, *, sweep_previous: bool = True) -> ScratchService:
        seen.append((dsn, sweep_previous))
        return cast("ScratchService", marker)

    backend = Backend(scratch_factory=factory)
    assert backend.scratch("fake://a") is marker
    assert backend.scratch("fake://b", sweep_previous=False) is marker
    assert seen == [("fake://a", True), ("fake://b", False)]


def test_a_backend_without_a_memory_factory_has_no_memory_urls() -> None:
    assert Backend().in_memory("fake::memory:") is False


def test_the_memory_factory_decides_which_urls_are_in_memory() -> None:
    backend = Backend(memory_factory=lambda dsn: dsn.endswith(":memory:"))
    assert backend.in_memory("fake::memory:") is True
    assert backend.in_memory("fake:///var/db") is False


def test_a_backend_has_no_compat_warning_by_default() -> None:
    assert Backend().compat_warning is None


def test_the_checker_vocabulary_defaults_to_the_postgresql_one() -> None:
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
    assert checker.json_keys == frozenset({"json", "jsonb"})
    assert checker.array_key == "array"
    assert checker.placeholder is dollar_placeholder
    assert checker.cast is text_cast
    assert checker.describer == "the server"
    assert (checker.untyped_params, checker.untyped_columns) == (False, False)
    assert (checker.placeholder(3), checker.cast("{x}")) == ("$3", "{x}::text")

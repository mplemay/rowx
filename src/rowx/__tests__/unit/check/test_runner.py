import asyncio
from dataclasses import dataclass, replace
from time import perf_counter
from typing import TYPE_CHECKING, override

import pytest
from rowx_core import Compiler
from rowx_core.backend import CheckerBackend, CheckerSession
from rowx_core.errors import DatabaseError, InterfaceError

from rowx.check.cli import main
from rowx.check.runner import CheckError, describe_all, redact

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from rowx_core import Compiled, TemplatePlan
    from rowx_core.backend import ColumnInfo, Describe, Failure, ParamInfo, ShapeIssue


class UnusedCompiler(Compiler):
    @override
    def compile(self, _plan: TemplatePlan, /) -> tuple[Compiled, list[object]]:
        message = "describe workers do not compile templates"
        raise AssertionError(message)

    @override
    def compile_parts(self, _strings: Sequence[str], /) -> Compiled:
        message = "describe workers do not compile templates"
        raise AssertionError(message)


def unused_mapping(
    *_args: object,
) -> tuple[Sequence[int | None], Sequence[ShapeIssue]]:
    message = "describe workers do not map row fields"
    raise AssertionError(message)


class FakeConnection(CheckerSession):
    closed_count = 0

    @override
    async def close(self) -> None:
        type(self).closed_count += 1

    @override
    async def describe(self, sql: str) -> Describe:
        message = "the test must supply describe outcomes"
        raise AssertionError(message)

    @override
    def classify(self, error: Exception) -> Failure:
        if isinstance(error, InterfaceError):
            return "lost"
        if isinstance(error, DatabaseError):
            if error.sqlstate == "57014":
                return "timeout"
            if error.sqlstate == "57P01" or error.sqlstate.startswith("08"):
                return "lost"
        return "other"


@pytest.fixture
def fake_backend() -> CheckerBackend:
    FakeConnection.closed_count = 0

    async def connect(_dsn: str, _timeout_s: float) -> FakeConnection:  # ruff: ignore[unused-async]
        return FakeConnection()

    return CheckerBackend(
        connect_checker=connect,
        compiler=UnusedCompiler(),
        map_columns=unused_mapping,
        types={},
        param_classes={},
        value_leaves={},
        range_leaves={},
        range_classes=frozenset(),
        value_classes={},
    )


def _describe_with(monkeypatch: pytest.MonkeyPatch, *outcomes: object) -> list[str]:
    seen: list[str] = []
    queue = list(outcomes)

    async def fake(_conn: object, sql: str) -> object:  # ruff: ignore[unused-async]
        seen.append(sql)
        outcome = queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(FakeConnection, "describe", fake)
    return seen


def test_lost_connection_is_retried_once(monkeypatch: pytest.MonkeyPatch, fake_backend: CheckerBackend) -> None:
    seen = _describe_with(monkeypatch, DatabaseError("57P01", "terminating connection"), "described")
    results = asyncio.run(describe_all("postgresql://x", ["SELECT 1"], 1, 1000, backend=fake_backend))
    assert results == {"SELECT 1": "described"}
    assert seen == ["SELECT 1", "SELECT 1"]
    assert FakeConnection.closed_count == 2


def test_connection_class_errors_are_retried(monkeypatch: pytest.MonkeyPatch, fake_backend: CheckerBackend) -> None:
    _describe_with(monkeypatch, DatabaseError("08006", "connection failure"), InterfaceError("closed"), "described")
    with pytest.raises(CheckError, match="lost the database connection"):
        asyncio.run(describe_all("postgresql://x", ["SELECT 1"], 1, 1000, backend=fake_backend))


def test_second_loss_fails_the_run(monkeypatch: pytest.MonkeyPatch, fake_backend: CheckerBackend) -> None:
    _describe_with(monkeypatch, InterfaceError("closed"), DatabaseError("57P01", "gone"))
    with pytest.raises(CheckError, match="lost the database connection"):
        asyncio.run(describe_all("postgresql://x", ["SELECT 1"], 1, 1000, backend=fake_backend))


def test_timeout_fails_the_run(monkeypatch: pytest.MonkeyPatch, fake_backend: CheckerBackend) -> None:
    _describe_with(monkeypatch, DatabaseError("57014", "canceling statement due to statement timeout"))
    with pytest.raises(CheckError, match="timed out"):
        asyncio.run(describe_all("postgresql://x", ["SELECT 1"], 1, 1000, backend=fake_backend))


def test_sql_errors_are_outcomes_not_failures(monkeypatch: pytest.MonkeyPatch, fake_backend: CheckerBackend) -> None:
    error = DatabaseError("42P01", "relation does not exist")
    _describe_with(monkeypatch, error)
    assert asyncio.run(describe_all("postgresql://x", ["SELECT 1"], 1, 1000, backend=fake_backend)) == {
        "SELECT 1": error
    }


def test_each_sql_is_described_once_across_workers(
    monkeypatch: pytest.MonkeyPatch, fake_backend: CheckerBackend
) -> None:
    sqls = [f"SELECT {n}" for n in range(9)]
    seen = _describe_with(monkeypatch, *[f"d{n}" for n in range(9)])
    results = asyncio.run(describe_all("postgresql://x", sqls, 4, 1000, backend=fake_backend))
    assert sorted(seen) == sorted(sqls)
    assert set(results) == set(sqls)


@dataclass(slots=True, kw_only=True, frozen=True)
class FakeDescribe:
    owner: str
    params: tuple[ParamInfo, ...] = ()
    columns: tuple[ColumnInfo, ...] = ()


@dataclass(slots=True, kw_only=True)
class OwnedSession(CheckerSession):
    owner: str
    started: list[str]
    both_started: asyncio.Event
    closed: bool = False

    @override
    async def describe(self, sql: str) -> FakeDescribe:
        self.started.append(self.owner)
        if len(self.started) == 2:
            self.both_started.set()
        await self.both_started.wait()
        return FakeDescribe(owner=self.owner)

    @override
    async def close(self) -> None:
        self.closed = True

    @override
    def classify(self, error: Exception) -> Failure:
        return "other"


def test_concurrent_backend_sessions_cannot_cross_route(fake_backend: CheckerBackend) -> None:
    async def run() -> None:
        started: list[str] = []
        both_started = asyncio.Event()
        first = OwnedSession(owner="first", started=started, both_started=both_started)
        second = OwnedSession(owner="second", started=started, both_started=both_started)

        async def connect_first(_dsn: str, _timeout: float) -> OwnedSession:  # ruff: ignore[unused-async]
            return first

        async def connect_second(_dsn: str, _timeout: float) -> OwnedSession:  # ruff: ignore[unused-async]
            return second

        outcomes = await asyncio.gather(
            describe_all("fake://first", ["SELECT 1"], 1, 1000, replace(fake_backend, connect_checker=connect_first)),
            describe_all("fake://second", ["SELECT 1"], 1, 1000, replace(fake_backend, connect_checker=connect_second)),
        )
        for outcome, owner in zip(outcomes, ("first", "second"), strict=True):
            described = outcome["SELECT 1"]
            assert isinstance(described, FakeDescribe)
            assert described.owner == owner
        assert first.closed
        assert second.closed

    asyncio.run(run())


@pytest.mark.parametrize(
    ("dsn", "secret"),
    [
        ("postgresql://user:p%40ss@host/db", "p%40ss"),
        ("postgresql://user:p%40ss@host/db", "p@ss"),
        ("host=h user=u password=p@ss dbname=d", "p@ss"),
        ("host=h user=u password='p ss' dbname=d", "p ss"),
        ("postgresql://u@h/db?password=pass&sslmode=require", "pass"),
    ],
)
def test_redact_removes_passwords(dsn: str, secret: str) -> None:
    redacted = redact(f"failed for {dsn} with {secret}", dsn)
    assert secret not in redacted
    assert dsn not in redacted


@pytest.mark.parametrize(
    ("dsn", "secrets"),
    [
        ("postgresql://user:p%40ss@host:notaport/db", ["p%40ss", "p@ss"]),
        ("postgresql://user@host/db?password=p%40ss&sslpassword=q%20r", ["p%40ss", "p@ss", "q%20r", "q r"]),
        ("host=h user=u password='p\\'ss' sslpassword=q\\ r", ["p\\'ss", "p'ss", "q\\ r", "q r"]),
        ("postgresql://user:p%40ss@[unparseable", ["p%40ss", "p@ss"]),
        ("host=h password='p@ss", ["p@ss"]),
    ],
)
def test_redact_covers_every_form_and_unparseable_dsns(dsn: str, secrets: list[str]) -> None:
    for secret in secrets:
        assert secret not in redact(f"boom {secret} in {dsn}", dsn)


def test_redact_covers_lowercase_percent_encoding() -> None:
    out = redact("boom p%c3%a4sss and p%C3%A4sss and pässs", "postgresql://u:pässs@h/db")
    assert "p%c3%a4" not in out
    assert "p%C3%A4" not in out
    assert "pä" not in out


def test_redact_is_linear_in_a_long_dsn() -> None:
    dsn = "host=h password='" + "\\'" * 10**6
    start = perf_counter()
    assert redact("x", dsn) == "x"
    assert perf_counter() - start < 1


def test_redact_masks_userinfo_and_password_tokens_by_syntax() -> None:
    text = "see postgresql://u:zzz@h/db?sslpassword=yyy and host=h password='x y'"
    assert redact(text, "") == "see postgresql://u:***@h/db?sslpassword=*** and host=h password=***"


def test_redact_skips_empty_secrets() -> None:
    assert redact("plain text", "host=h password=") == "plain text"


def test_redact_merges_overlapping_secrets_before_masking() -> None:
    assert redact("abcdef", "host=h password=abcd sslpassword=cdef") == "***"
    assert redact("xx abcdef yy", "host=h password=abcd sslpassword=cdef") == "xx *** yy"
    assert redact("a*b", "host=h password=*") == "a***b"
    assert redact("ababa", "host=h password=aba") == "***"


def test_a_file_nested_too_deeply_does_not_hide_the_others(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "deep.py").write_text("y = " + " + ".join(["1"] * 3000) + "\n", encoding="utf-8")
    (tmp_path / "bad.py").write_text("from string.templatelib import Template\nTemplate(x)\n", encoding="utf-8")
    code = main(["check", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1
    assert "internal error" not in out
    assert "the file is nested too deeply to analyse" in out
    assert "template-construction" in out

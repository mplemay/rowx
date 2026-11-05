# TEMPORARY parity harness for #229, removed before merge: runs every describe through both plan passes
# (the EXPLAIN ... EXECUTE of the prepared statement, and the old concatenated EXPLAIN (GENERIC_PLAN) text)
# and records any difference. Load it as a plugin with ROWX_FUZZ_PARITY=1:
#   ROWX_FUZZ_PARITY=1 uv run pytest -p rowx.__tests__.integration.describe.parity <paths>
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

import pytest

from rowx._core import postgres
from rowx._core.postgres import Connection, Describe, _describe_concatenated

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

ENABLED: Final[bool] = os.environ.get("ROWX_FUZZ_PARITY") == "1"
describe_original: Final = postgres.describe

type Signature = tuple[object, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class Outcome:
    signature: Signature
    described: Describe | None = None
    error: BaseException | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Difference:
    sql: str
    execute_form: Signature
    concatenated_form: Signature


@dataclass(slots=True)
class Tally:
    calls: int = 0
    statements: set[str] = field(default_factory=set)
    differences: list[Difference] = field(default_factory=list)


TALLY: Final[Tally] = Tally()
PATCH: Final[pytest.MonkeyPatch] = pytest.MonkeyPatch()


async def outcome(run: Callable[[Connection, str], Awaitable[Describe]], conn: Connection, sql: str) -> Outcome:
    try:
        described = await run(conn, sql)
    except Exception as error:  # ruff: ignore[blind-except]
        return Outcome(signature=("error", type(error).__name__, str(error)), error=error)
    columns = [(c.name, c.nullable, c.reason, c.reason_detail) for c in described.columns]
    params = [(p.oid, p.type_name, p.type_key) for p in described.params]
    return Outcome(signature=("ok", params, columns), described=described)


async def compare(conn: Connection, sql: str) -> tuple[Outcome, Difference | None]:
    new = await outcome(describe_original, conn, sql)
    old = await outcome(_describe_concatenated, conn, sql)
    TALLY.calls += 1
    TALLY.statements.add(sql)
    if new.signature == old.signature:
        return new, None
    difference = Difference(sql=sql, execute_form=new.signature, concatenated_form=old.signature)
    TALLY.differences.append(difference)
    return new, difference


async def compared_describe(conn: Connection, sql: str) -> Describe:
    new, difference = await compare(conn, sql)
    if difference is not None:
        msg = f"PARITY DIFFERENCE for {sql!r}\n  execute:      {difference.execute_form}\n  concatenated: {difference.concatenated_form}"
        raise AssertionError(msg)
    if new.error is not None:
        raise new.error
    assert new.described is not None
    return new.described


def pytest_configure() -> None:
    if ENABLED:
        PATCH.setattr(postgres, "describe", compared_describe)


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    if not ENABLED:
        return
    terminalreporter.section("describe parity (EXPLAIN EXECUTE vs concatenated)")
    terminalreporter.line(f"describe calls compared: {TALLY.calls}")
    terminalreporter.line(f"distinct statements compared: {len(TALLY.statements)}")
    terminalreporter.line(f"differences: {len(TALLY.differences)}")
    for difference in TALLY.differences:
        terminalreporter.line(f"DIFF {difference.sql!r}")
        terminalreporter.line(f"  execute:      {difference.execute_form}")
        terminalreporter.line(f"  concatenated: {difference.concatenated_form}")


def pytest_sessionfinish(session: pytest.Session) -> None:
    if ENABLED and TALLY.differences and session.exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED

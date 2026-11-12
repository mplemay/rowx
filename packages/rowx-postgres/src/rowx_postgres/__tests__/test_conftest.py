import os
import re
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
from pathlib import Path
from typing import Final

import psycopg
import pytest

pytestmark = [pytest.mark.embedded]

CHILD_TIMEOUT: Final[float] = 300.0
TESTS_DIR: Final[Path] = Path(__file__).resolve().parent
SKIP_LINE: Final[re.Pattern[str]] = re.compile(r"^SKIPPED \[(\d+)\] \S+: (.*)$", re.MULTILINE)
SERVER_SKIP_REASON: Final[re.Pattern[str]] = re.compile(r"^(needs a server: needs \S+|embedded engine divergence \S+)")
PROBE_ENV: Final[str] = "ROWX_META_PROBE_SKIP"
LEAK_ENV: Final[str] = "ROWX_META_PROBE_LEAK"
LEAKED: Final[list[psycopg.Connection[tuple[object, ...]]]] = []


# the runtime skip below exists only for the child run that proves the lane turns an unmarked skip into a failure
def test_probe_an_unmarked_skip_fails_the_lane() -> None:
    if os.environ.get(PROBE_ENV) == "1":
        pytest.skip("probe: a precondition nobody marked")


# the two probes below only act in the child run that proves the guard: the first keeps a session open, as a test that
# forgot to close one would, and the second is the next test on the same worker
@pytest.mark.db
def test_probe_a_test_that_leaves_a_session_open() -> None:
    if os.environ.get(LEAK_ENV) == "1":
        LEAKED.append(psycopg.connect(os.environ["ROWX_TEST_DSN"], autocommit=True))


@pytest.mark.db
def test_probe_the_test_after_the_leak() -> None:
    assert True


# a fresh pytest process on an embedded DSN, because only a run of its own shows what the swap really does
def _child(
    *args: str, probe: bool = False, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key not in {"PYTEST_XDIST_WORKER", "PYTEST_ADDOPTS"}}
    env["ROWX_TEST_DSN"] = "embedded://"
    env.pop(PROBE_ENV, None)
    if probe:
        env[PROBE_ENV] = "1"
    env.update(extra_env or {})
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=CHILD_TIMEOUT,
    )


VERBOSE_SKIP: Final[re.Pattern[str]] = re.compile(r"^(\S+::.*?) SKIPPED", re.MULTILINE)


def test_a_child_run_skips_every_server_test_with_a_server_reason_and_nothing_else() -> None:
    ran = _child("-m", "server", "-v", "-rs", str(TESTS_DIR))
    skipped_ids = set(VERBOSE_SKIP.findall(ran.stdout))
    expected = len(skipped_ids)
    assert expected > 0, ran.stdout[-2000:]
    skipped = SKIP_LINE.findall(ran.stdout)
    assert sum(int(count) for count, _ in skipped) == expected, ran.stdout[-2000:]
    assert [reason for _, reason in skipped if not SERVER_SKIP_REASON.match(reason)] == []
    summary = ran.stdout.strip().splitlines()[-1]
    assert " skipped" in summary
    for outcome in ("passed", "failed", "error", "xfailed", "xpassed"):
        assert outcome not in summary, summary
    # `timing` implies `server`, so every timing test is among the ones the swap skipped
    timing = {
        line
        for line in _child("-m", "timing", "--collect-only", "-q", str(TESTS_DIR)).stdout.splitlines()
        if "::" in line
    }
    assert timing
    assert timing <= skipped_ids


def test_the_lane_fails_a_test_that_skips_at_run_time_without_being_marked_server() -> None:
    probe = f"{Path(__file__).name}::test_probe_an_unmarked_skip_fails_the_lane"
    quiet = _child(str(TESTS_DIR / probe))
    assert "1 passed" in quiet.stdout, quiet.stdout[-1000:]
    loud = _child(str(TESTS_DIR / probe), probe=True)
    assert "1 failed" in loud.stdout, loud.stdout[-1000:]
    assert "unmarked skip on the embedded lane" in loud.stdout


def test_a_test_that_leaves_a_session_open_is_named_and_the_next_one_fails_in_one_line() -> None:
    probes = [
        str(TESTS_DIR / "test_conftest.py::test_probe_a_test_that_leaves_a_session_open"),
        str(TESTS_DIR / "test_conftest.py::test_probe_the_test_after_the_leak"),
    ]
    quiet = _child(*probes)
    assert "2 passed" in quiet.stdout, quiet.stdout[-1000:]
    env_leak = {LEAK_ENV: "1"}
    loud = _child(*probes, extra_env=env_leak)
    assert "this test left a session open on the embedded database" in loud.stdout, loud.stdout[-1500:]
    assert "test_probe_a_test_that_leaves_a_session_open" in loud.stdout
    assert "left open" in loud.stdout
    assert "1 passed, 2 errors" in loud.stdout, loud.stdout[-1500:]

import os

import pytest

from rowx.__tests__.nullgen import fuzz_settings, run_fuzz

pytestmark = [pytest.mark.integration, pytest.mark.db]


# gating: a column that describe calls NOT NULL is never NULL in any result. The default seed and count are fixed;
# ROWX_FUZZ_SEED and ROWX_FUZZ_COUNT override them, and a failure prints both with the statement
def test_not_null_verdicts_are_sound(schema: str) -> None:
    seed, count = fuzz_settings()
    report = run_fuzz(schema, seed, count)
    assert not report.failures, report.render()
    assert report.statements == count


# non-gating: set ROWX_FUZZ_REPORT=1 to print how many nullable verdicts never saw a NULL, by reason
@pytest.mark.skipif(os.environ.get("ROWX_FUZZ_REPORT") != "1", reason="ROWX_FUZZ_REPORT is not 1")
def test_precision_report(schema: str) -> None:
    seed, count = fuzz_settings()
    report = run_fuzz(schema, seed, count)
    print(report.render())  # ruff: ignore[print]

# TEMPORARY (#229, removed before merge): the two plan passes agree on the nullgen fuzz corpus
import asyncio
import os
import random

import pytest

from rowx.__tests__.integration.describe.parity import compare, compared_describe
from rowx.__tests__.nullgen import fuzz_settings, generate
from rowx._core import postgres
from rowx._core.postgres import connect_checker

pytestmark = [
    pytest.mark.integration,
    pytest.mark.db,
    pytest.mark.skipif(os.environ.get("ROWX_FUZZ_PARITY") != "1", reason="ROWX_FUZZ_PARITY is not 1"),
]


def test_the_parity_plugin_wraps_describe() -> None:
    # fails when the run forgot `-p rowx.__tests__.integration.describe.parity`
    assert postgres.describe is compared_describe


def test_fuzz_corpus_has_identical_verdicts_in_both_forms(schema: str) -> None:
    seed, count = fuzz_settings()
    rng = random.Random(seed)  # ruff: ignore[suspicious-non-cryptographic-random-usage]
    statements = [generate(rng).sql for _ in range(count)]

    async def run() -> list[str]:
        conn = await connect_checker(schema)
        try:
            found: list[str] = []
            for sql in statements:
                _, difference = await compare(conn, sql)
                if difference is not None:
                    found.append(
                        f"{sql}\n  execute:      {difference.execute_form}\n  concatenated: {difference.concatenated_form}"
                    )
            return found
        finally:
            await conn.close()

    differences = asyncio.run(run())
    summary = f"parity fuzz: seed={seed} count={count} compared={len(statements)} differences={len(differences)}"
    print(summary)  # ruff: ignore[print]
    assert not differences, "\n".join(differences)

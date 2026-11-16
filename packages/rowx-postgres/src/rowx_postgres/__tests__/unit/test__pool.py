import asyncio

import pytest
from rowx_core.errors import InterfaceError, PoolTimeoutError
from rowx_postgres import Pool

UNREACHABLE_DSN = "postgresql://rowx:rowx@127.0.0.1:1/rowx"


def test_a_pool_timeout_carries_the_last_connect_error_as_its_cause() -> None:
    async def main() -> PoolTimeoutError:
        pool = Pool(UNREACHABLE_DSN, max_size=1, acquire_timeout=0.5)
        try:
            with pytest.raises(PoolTimeoutError) as caught:
                async with pool.acquire():
                    pass
        finally:
            await pool.close()
        return caught.value

    error = asyncio.run(main())
    assert isinstance(error.__cause__, InterfaceError)
    assert "the last connect failed" in str(error)
    assert str(error.__cause__) in str(error)


def test_pool_bounds_are_checked_before_the_url_is_read() -> None:
    with pytest.raises(ValueError, match="max_size"):
        Pool("not a url at all :: ??", max_size=0)

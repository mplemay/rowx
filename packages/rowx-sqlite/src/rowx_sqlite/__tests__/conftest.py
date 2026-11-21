from typing import TYPE_CHECKING

import pytest_asyncio
from rowx_sqlite import Connection, ConnectOptions, connect

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@pytest_asyncio.fixture
async def conn(tmp_path: Path) -> AsyncIterator[Connection]:
    connection = await connect(ConnectOptions(path=str(tmp_path / "test.db"), mode="rwc"))
    try:
        yield connection
    finally:
        await connection.close()

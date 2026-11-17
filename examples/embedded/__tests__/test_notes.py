import asyncio
from typing import TYPE_CHECKING

from notes import add_note, delete_note, get_note, list_notes, tag_counts, tag_note, titles_tagged

if TYPE_CHECKING:
    from rowx_postgres import Pool

# The fixtures are synchronous, so these tests drive the async code with `asyncio.run`. With pytest-asyncio (or anyio
# on its asyncio backend) the tests can be `async def` instead. Every test gets its own database, booted from a snapshot
# of the migrated schema, so none of them sees another's rows.


def test_each_test_starts_from_the_migrated_schema(rowx_pool: Pool) -> None:
    assert asyncio.run(list_notes(rowx_pool)) == []


def test_a_note_is_stored_and_found(rowx_pool: Pool) -> None:
    async def work() -> None:
        note_id = await add_note(rowx_pool, "Buy milk")
        assert await get_note(rowx_pool, note_id) == (note_id, "Buy milk", None)
        assert await get_note(rowx_pool, note_id + 1) is None

    asyncio.run(work())


def test_tags_group_notes(rowx_pool: Pool) -> None:
    async def work() -> None:
        first = await add_note(rowx_pool, "Buy milk")
        second = await add_note(rowx_pool, "Read the PEP", "PEP 750")
        for note_id, tag in [(first, "errand"), (second, "errand"), (second, "reading"), (second, "reading")]:
            await tag_note(rowx_pool, note_id, tag)
        assert await titles_tagged(rowx_pool, "errand") == ["Buy milk", "Read the PEP"]
        assert await tag_counts(rowx_pool) == [("errand", 2), ("reading", 1)]

    asyncio.run(work())


def test_deleting_a_note_removes_its_tags(rowx_pool: Pool) -> None:
    async def work() -> None:
        note_id = await add_note(rowx_pool, "Buy milk")
        await tag_note(rowx_pool, note_id, "errand")
        assert await delete_note(rowx_pool, note_id) == 1
        assert await tag_counts(rowx_pool) == []

    asyncio.run(work())

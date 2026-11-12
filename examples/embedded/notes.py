import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING, Final

from rowx_postgres import Migrator
from rowx_postgres.embedded import Database

from rowx import query

if TYPE_CHECKING:
    from rowx import Executor

MIGRATIONS: Final[Path] = Path(__file__).resolve().parent / "migrations"
# no server needed: the database starts an in-process Postgres on first use and closes it with the database
DIRECTORY_ENV: Final[str] = "NOTES_DB_DIR"

type NoteRow = tuple[int, str, str | None]  # id, title, body


async def add_note(db: Executor, title: str, body: str | None = None) -> int:
    return await query[int](t"INSERT INTO notes (title, body) VALUES ({title}, {body}) RETURNING id").one(db)


async def tag_note(db: Executor, note_id: int, tag: str) -> None:
    await query(t"INSERT INTO note_tags (note_id, tag) VALUES ({note_id}, {tag}) ON CONFLICT DO NOTHING").execute(db)


async def get_note(db: Executor, note_id: int) -> NoteRow | None:
    return await query[NoteRow](t"SELECT id, title, body FROM notes WHERE id = {note_id}").optional(db)


async def list_notes(db: Executor) -> list[NoteRow]:
    return await query[NoteRow](t"SELECT id, title, body FROM notes ORDER BY id").all(db)


async def titles_tagged(db: Executor, tag: str) -> list[str]:
    return await query[str](
        t"""
        SELECT n.title FROM notes n JOIN note_tags t ON t.note_id = n.id
        WHERE t.tag = {tag} ORDER BY n.title
        """
    ).all(db)


async def tag_counts(db: Executor) -> list[tuple[str, int]]:
    return await query[tuple[str, int]](
        t'SELECT tag, count(*) AS "n!" FROM note_tags GROUP BY tag ORDER BY "n!" DESC, tag'
    ).all(db)


async def delete_note(db: Executor, note_id: int) -> int:
    return (await query(t"DELETE FROM notes WHERE id = {note_id}").execute(db)).rows_affected


async def main(directory: str | None) -> None:
    # reads and validates the files now, with no database IO
    migrator = Migrator(MIGRATIONS)
    async with Database(directory) as pool:
        for result in await migrator.run(pool):
            print(f"applied {result.migration.version} {result.migration.description}")  # ruff: ignore[print]

        first = await add_note(pool, "Buy milk")
        second = await add_note(pool, "Read the PEP", "PEP 750")
        await tag_note(pool, first, "errand")
        await tag_note(pool, second, "reading")
        await tag_note(pool, second, "errand")

        for note_id, title, body in await list_notes(pool):
            print(f"{note_id}: {title} ({body or 'no body'})")  # ruff: ignore[print]
        print("errands:", ", ".join(await titles_tagged(pool, "errand")))  # ruff: ignore[print]
        for tag, count in await tag_counts(pool):
            print(f"{tag}: {count}")  # ruff: ignore[print]


if __name__ == "__main__":
    # unset is a fresh in-memory database; an absolute directory keeps it between runs
    asyncio.run(main(os.environ.get(DIRECTORY_ENV) or None))

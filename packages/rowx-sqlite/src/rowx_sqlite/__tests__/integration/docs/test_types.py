from pathlib import Path
from typing import Final

from rowx_sqlite._core import type_table

ROOT: Final[Path] = next(parent for parent in Path(__file__).resolve().parents if (parent / "Cargo.lock").is_file())
TYPES_DOC: Final[Path] = ROOT / "docs" / "types.md"
HEADING: Final[str] = "## SQLite\n"
NOTES_HEADING: Final[str] = "## SQLite notes\n"


def _table_rows() -> dict[str, str]:
    section = TYPES_DOC.read_text().split(HEADING, 1)[1].split(NOTES_HEADING, 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("| `")]
    # a pipe inside a cell is escaped, so it is not a column separator
    return {line.split("`", 2)[1]: line.replace("\\|", "|") for line in rows}


# the page itself is checked by the generator (`scripts/gen-types-doc.py --check`, run by the PostgreSQL docs test); this
# one pins that the SQLite section lists exactly the keys of the table the codecs and the checker read
def test_the_sqlite_section_lists_every_key_of_the_type_table() -> None:
    rows = _table_rows()
    assert list(rows) == [info.key for info in type_table()]
    for info in type_table():
        assert f"`{info.py_result}`" in rows[info.key]
        assert all(f"`{name}`" in rows[info.key] for name in info.accepted_params)

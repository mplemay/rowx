from dataclasses import dataclass
from typing import TYPE_CHECKING

from rowx_postgres._checker import override_issues, quoted_aliases
from rowx_postgres._compiler import IDENT_MAX_BYTES

if TYPE_CHECKING:
    from rowx_core.backend import NullReason


def test_aliases_come_only_from_as_clauses() -> None:
    sql = (
        'SELECT 1 AS "one!", 2 AS /* c */ "two?", "t"."col", \'AS "str!"\' AS x, $$ AS "dollar!" $$ AS y, '
        '3 -- AS "line!"\n AS "three!" /* AS "block!" /* nested */ AS "inner!" */, 4 AS "fo""ur!"'
    )
    assert [ident for _, ident in quoted_aliases(sql)] == ["one!", "two?", "three!", 'fo"ur!']
    assert [sql[offset] for offset, _ in quoted_aliases(sql)] == ['"'] * 4


@dataclass(slots=True, kw_only=True, frozen=True)
class _DescribedColumn:
    name: str
    type_name: str
    type_key: str | None
    nullable: bool
    reason: NullReason
    reason_detail: str | None
    element_key: str | None
    raw_name: str


def _column(raw_name: str) -> _DescribedColumn:
    return _DescribedColumn(
        name=raw_name,
        type_name="int4",
        type_key="int4",
        nullable=False,
        reason="Computed",
        reason_detail=None,
        element_key=None,
        raw_name=raw_name,
    )


def test_override_issues_reports_only_a_truncated_suffix() -> None:
    described_60 = "a" * 60
    exact_63 = "b" * (IDENT_MAX_BYTES - 1) + "!"
    described_59 = "c" * (IDENT_MAX_BYTES - 4)
    lost = described_60 + "xxxx!"
    short_alias = described_59 + "yyyy!"
    assert len(described_60.encode()) == 60
    assert len(lost.encode()) > IDENT_MAX_BYTES
    assert len(exact_63.encode()) == IDENT_MAX_BYTES
    assert exact_63.endswith("!")
    assert len(described_59.encode()) == IDENT_MAX_BYTES - 4
    assert len(short_alias.encode()) > IDENT_MAX_BYTES
    sql = f'SELECT 1 AS "{lost}", 2 AS "{exact_63}", 3 AS "{short_alias}"'
    issues = override_issues(sql, (_column(described_60), _column(exact_63), _column(described_59)))
    assert [issue.message for issue in issues] == [
        (f'column 1 "{described_60}" was truncated at {IDENT_MAX_BYTES} bytes, so its "!" or "?" override is lost'),
    ]

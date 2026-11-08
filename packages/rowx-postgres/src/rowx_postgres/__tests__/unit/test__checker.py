from rowx_postgres._checker import quoted_aliases


def test_aliases_come_only_from_as_clauses() -> None:
    sql = (
        'SELECT 1 AS "one!", 2 AS /* c */ "two?", "t"."col", \'AS "str!"\' AS x, $$ AS "dollar!" $$ AS y, '
        '3 -- AS "line!"\n AS "three!" /* AS "block!" /* nested */ AS "inner!" */, 4 AS "fo""ur!"'
    )
    assert [ident for _, ident in quoted_aliases(sql)] == ["one!", "two?", "three!", 'fo"ur!']
    assert [sql[offset] for offset, _ in quoted_aliases(sql)] == ['"'] * 4

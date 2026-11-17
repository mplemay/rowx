import rowx_core

import rowx
import rowx.core
import rowx.errors


def test_core_facade_reexports_only_names_missing_elsewhere() -> None:
    exported = set(rowx.core.__all__)
    already = set(rowx.__all__) | set(rowx.errors.__all__)
    assert exported == set(rowx_core.__all__) - already
    assert exported
    assert all(getattr(rowx.core, name) is getattr(rowx_core, name) for name in exported)
    assert rowx.query is rowx_core.query
    assert rowx.RowNotFound is rowx.errors.RowNotFound is rowx_core.RowNotFound
    assert "connect" not in rowx.core.__all__
    assert "connect" not in rowx_core.__all__

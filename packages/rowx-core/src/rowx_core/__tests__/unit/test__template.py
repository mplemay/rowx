from typing import override

from rowx_core import Identifier, capture_template, iter_parts


def test_empty_nested_templates_do_not_expand_during_compilation() -> None:
    template = t""
    for _ in range(100):
        template = t"{template}{template}"
    plan = capture_template(template, dynamic=True)
    assert plan.parts == ()
    assert list(iter_parts(plan)) == []


def test_identifier_capture_snapshots_strings_before_compilation() -> None:
    # Exercise the exact builtin-subclass boundary accepted by identifier interpolation.
    class Name(str):  # ruff: ignore[subclass-builtin]
        __slots__ = ()

        @override
        def __str__(self) -> str:
            msg = "str override must not run"
            raise AssertionError(msg)

    name = Name("table")
    plan = capture_template(t"SELECT * FROM {name:ident}", dynamic=True)
    identifiers = [part for part in iter_parts(plan) if isinstance(part, Identifier)]
    assert identifiers == [Identifier(parts=("table",))]

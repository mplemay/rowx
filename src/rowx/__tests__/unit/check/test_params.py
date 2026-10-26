import ast
from textwrap import dedent
from typing import Final

import pytest

from rowx.__tests__.paramgen import (
    DECOYS,
    POISONS,
    build,
    direct,
    infer as infer_case,
    run,
    runtime_shown,
    shown,
    unknown_expression,
)
from rowx.__tests__.sourcegen import seeded
from rowx.check.params import ParamInference, ParamType


def show(found: ParamType | None) -> str | None:
    if found is None:
        return None
    return f"list[{show(found.item)}]" if found.name == "list" else found.name


def infer(source: str) -> list[str | None]:
    tree = ast.parse(dedent(source))
    inference = ParamInference(tree)
    return [
        show(found)
        for node in sorted(ast.walk(tree), key=lambda n: (getattr(n, "lineno", 0), getattr(n, "col_offset", 0)))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "q"
        for arg in node.args
        if isinstance(arg, ast.TemplateStr)
        for found in inference.interpolations(arg)
    ]


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("1", "int"),
        ("-1", "int"),
        ("1.5", "float"),
        ("-1.5", "float"),
        ("'a'", "str"),
        ("b'a'", "bytes"),
        ("True", "bool"),
        ("None", "None"),
        ("t'{1}'", "Template"),
        ("[1, 2]", "list[int]"),
        ("(1, 2)", "list[int]"),
        ("['a']", "list[str]"),
        ("[[1], [2]]", "list[list[int]]"),
        ("[1, 'a']", None),
        ("[1, None]", None),
        ("[1, *xs]", None),
        ("[]", None),
        ("[x]", None),
        ("1j", None),
        ("unknown", None),
        ("f()", None),
        ("a.b", None),
    ],
)
def test_literals(expr: str, expected: str | None) -> None:
    assert infer(f't = q(t"{{{expr}}}")') == [expected]


def test_bool_is_not_int() -> None:
    assert infer('q(t"{True} {1}")') == ["bool", "int"]


@pytest.mark.parametrize(
    ("imports", "expr", "expected"),
    [
        ("import decimal", "decimal.Decimal('1')", "Decimal"),
        ("from decimal import Decimal", "Decimal(1)", "Decimal"),
        ("from decimal import Decimal as D", "D(1)", "Decimal"),
        ("import uuid", "uuid.UUID(int=1)", "UUID"),
        ("import uuid as u", "u.UUID(int=1)", "UUID"),
        ("import datetime", "datetime.date(2020, 1, 1)", "date"),
        ("import datetime", "datetime.datetime(2020, 1, 1)", "datetime"),
        ("import datetime", "datetime.timedelta(days=1)", "timedelta"),
        ("import datetime", "datetime.datetime.now()", "datetime"),
        ("import datetime as dt", "dt.datetime.now(dt.UTC)", "datetime"),
        ("from datetime import datetime", "datetime.now()", "datetime"),
        ("from datetime import date", "date(2020, 1, 1)", "date"),
        ("from datetime import datetime", "datetime(2020, 1, 1)", "datetime"),
        ("from datetime import date", "[date(2020, 1, 1)]", "list[date]"),
        ("import datetime", "datetime.date.today()", None),
        ("import datetime", "datetime.time(1)", None),
        ("", "Decimal(1)", None),
        ("from other import Decimal", "Decimal(1)", None),
        ("from .decimal import Decimal", "Decimal(1)", None),
        ("from decimal import *", "Decimal(1)", None),
    ],
)
def test_constructors(imports: str, expr: str, expected: str | None) -> None:
    assert infer(f'{imports}\nq(t"{{{expr}}}")') == [expected]


def test_constructor_resolved_by_import_not_name() -> None:
    source = """
        from mylib import UUID
        import uuid
        q(t"{UUID(1)} {uuid.UUID(1)}")
    """
    assert infer(source) == [None, "UUID"]


def test_shadowed_import_is_unknown() -> None:
    source = """
        from decimal import Decimal
        def f(Decimal):
            q(t"{Decimal(1)}")
    """
    assert infer(source) == [None]


def test_annotated_parameter() -> None:
    source = """
        def f(a: int, b: str, c, d: float = 1.0, *, e: bytes):
            q(t"{a} {b} {c} {d} {e}")
    """
    assert infer(source) == ["int", "str", None, "float", "bytes"]


def test_variadic_parameters_are_unknown() -> None:
    source = """
        def f(*a: int, **b: int):
            q(t"{a} {b}")
    """
    assert infer(source) == [None, None]


def test_annotation_unions() -> None:
    source = """
        def f(a: int | None, b: None | str, c: int | str, d: int | str | None, e: None, g: int | int):
            q(t"{a} {b} {c} {d} {e} {g}")
    """
    assert infer(source) == ["int", "str", None, None, "None", "int"]


def test_annotation_lists() -> None:
    source = """
        def f(a: list[int], b: list[int | None], c: list, d: list[object], e: list[list[str]]):
            q(t"{a} {b} {c} {d} {e}")
    """
    assert infer(source) == ["list[int]", "list[int]", None, None, "list[list[str]]"]


def test_annotation_parameterized_range() -> None:
    source = """
        from rowx.postgres import Range

        def f(a: Range[int], b: list[Range[int]], c: Range[int] | None):
            q(t"{a} {b} {c}")
    """
    assert infer(source) == ["Range", "list[Range]", "Range"]


def test_annotation_imports() -> None:
    source = """
        import datetime
        from decimal import Decimal
        from uuid import UUID as Id
        def f(a: datetime.date, b: Decimal | None, c: Id, d: Mystery):
            q(t"{a} {b} {c} {d}")
    """
    assert infer(source) == ["date", "Decimal", "UUID", None]


def test_shadowed_builtin_annotation_is_unknown() -> None:
    source = """
        int = object
        def f(a: int):
            q(t"{a}")
    """
    assert infer(source) == [None]


def test_string_annotation_is_unknown() -> None:
    source = """
        def f(a: "int"):
            q(t"{a}")
    """
    assert infer(source) == [None]


def test_ann_assign_and_assign() -> None:
    source = """
        a: int = compute()
        b: str
        c = 5
        d = c
        e = [c]
        f = compute()
        q(t"{a} {b} {c} {d} {e} {f}")
    """
    assert infer(source) == ["int", "str", "int", "int", "list[int]", None]


def test_chained_assignment() -> None:
    assert infer('a = b = 1\nq(t"{a} {b}")') == ["int", "int"]


@pytest.mark.parametrize(
    "rebinding",
    [
        "x = 2",
        "x: int = 2",
        "x += 1",
        "for x in range(3): pass",
        "async for x in aiter(): pass",
        "with open('f') as x: pass",
        "(x := 3)",
        "x, y = 1, 2",
        "[x, *y] = 1, 2",
        "del x",
        "import x",
        "from m import x",
        "def x(): ...",
        "class x: ...",
        "type x = int",
        "match 1:\n    case x: pass",
        "match 1:\n    case [*x]: pass",
        "match 1:\n    case {**x}: pass",
        "try:\n    pass\nexcept Exception as x:\n    pass",
    ],
)
def test_second_binding_makes_name_unknown(rebinding: str) -> None:
    body = rebinding.replace("\n", "\n    ")
    source = f"def f():\n    x = 1\n    {body}\n    q(t'{{x}}')\n"
    assert infer(source) == [None]


def test_binding_in_branch_counts() -> None:
    source = """
        x = 1
        if cond():
            x = 2
        q(t"{x}")
    """
    assert infer(source) == [None]


def test_loop_target_and_with_alone_are_unknown() -> None:
    source = """
        for a in [1, 2]:
            q(t"{a}")
        with open("f") as b:
            q(t"{b}")
    """
    assert infer(source) == [None, None]


def test_walrus_alone_is_unknown() -> None:
    assert infer('if (a := 1):\n    q(t"{a}")') == [None]


def test_walrus_in_comprehension_binds_enclosing_scope() -> None:
    source = """
        def f():
            x = 1
            [(x := i) for i in range(3)]
            q(t"{x}")
    """
    assert infer(source) == [None]


def test_global_and_nonlocal() -> None:
    source = """
        x: int = 1
        y: int = 1
        def f():
            global x
            x = "a"
        def g():
            z: int = 1
            def h():
                nonlocal z
                z = "a"
            q(t"{z}")
        q(t"{x} {y}")
    """
    assert infer(source) == [None, None, "int"]


def test_globals_taint_only_declared_names() -> None:
    source = """
        x: int = 1
        y: int = 1
        def f():
            global x
            x = "a"
        q(t"{x} {y}")
    """
    assert infer(source) == [None, "int"]


def test_function_scope_shadows_module() -> None:
    source = """
        x = 1
        def f():
            x = "a"
            q(t"{x}")
        q(t"{x}")
    """
    assert infer(source) == ["str", "int"]


def test_function_scope_with_unknown_binding_does_not_fall_through() -> None:
    source = """
        x = 1
        def f():
            x = compute()
            q(t"{x}")
    """
    assert infer(source) == [None]


def test_closure_reads_enclosing_function() -> None:
    source = """
        def outer(a: int):
            def inner():
                q(t"{a}")
    """
    assert infer(source) == ["int"]


def test_closure_reads_module() -> None:
    source = """
        LIMIT = 10
        def f():
            q(t"{LIMIT}")
    """
    assert infer(source) == ["int"]


def test_class_body_name_invisible_in_method() -> None:
    source = """
        class C:
            x = 1
            q(t"{x}")
            def m(self):
                q(t"{x}")
    """
    assert infer(source) == ["int", None]


def test_class_body_name_does_not_leak_to_module() -> None:
    source = """
        x = "a"
        class C:
            x = 1
            def m(self):
                q(t"{x}")
    """
    assert infer(source) == ["str"]


def test_comprehension_scope_is_isolated() -> None:
    source = """
        i = 1
        [q(t"{i}") for i in ["a"]]
        q(t"{i}")
    """
    assert sorted(map(str, infer(source))) == ["None", "int"]


def test_comprehension_variable_does_not_make_outer_name_unknown() -> None:
    source = """
        x = 1
        [x for x in range(3)]
        q(t"{x}")
    """
    assert infer(source) == ["int"]


def test_comprehension_first_iterable_uses_enclosing_scope() -> None:
    source = """
        xs = [1]
        [q(t"{xs}") for xs in [q(t"{xs}")]]
    """
    assert sorted(map(str, infer(source))) == ["None", "list[int]"]


def test_lambda_scope() -> None:
    source = """
        x = 1
        f = lambda x: q(t"{x}")
        g = lambda: q(t"{x}")
    """
    assert sorted(map(str, infer(source))) == ["None", "int"]


def test_self_reference_is_unknown() -> None:
    assert infer('x = x\nq(t"{x}")') == [None]


def test_mutual_reference_is_unknown() -> None:
    assert infer('a = b\nb = a\nq(t"{a} {b}")') == [None, None]


def test_star_import_makes_builtins_unknown() -> None:
    source = """
        from m import *
        def f(a: int):
            q(t"{a}")
    """
    assert infer(source) == [None]


def test_attribute_target_is_not_a_name_binding() -> None:
    source = """
        def f(self, x: int):
            self.x = 2
            q(t"{x}")
    """
    assert infer(source) == ["int"]


def test_async_function_and_nested_template() -> None:
    source = """
        async def f(a: int):
            return q(t"{a} {t'{a}'}")
    """
    assert infer(source) == ["int", "Template"]


def test_format_spec_interpolations_are_not_parameters() -> None:
    assert infer('w = 5\nq(t"{1:{w}}")') == ["int"]


def test_type_parameter_shadows_builtin_annotation() -> None:
    source = """
        def f[int](x: int, y: str) -> int:
            q(t"{x} {y}")
        class C[str]:
            def m(self, z: str):
                q(t"{z}")
    """
    assert infer(source) == [None, "str", None]


def test_type_parameter_does_not_leak_to_default_or_decorator_scope() -> None:
    source = """
        def f[T](x: T, y: int = 1):
            q(t"{x} {y}")
    """
    assert infer(source) == [None, "int"]


@pytest.mark.parametrize(
    ("imports", "annotation", "expected"),
    [
        ("import builtins", "builtins.int", "int"),
        ("from builtins import int as I", "I", "int"),
        ("from builtins import str", "str", "str"),
        ("import builtins", "builtins.list[builtins.int]", "list[int]"),
        ("import builtins as b", "b.bytes | None", "bytes"),
    ],
)
def test_qualified_builtin_annotations(imports: str, annotation: str, expected: str) -> None:
    assert infer(f'{imports}\ndef f(x: {annotation}):\n    q(t"{{x}}")') == [expected]


SEEDS: Final = range(300)


@pytest.mark.parametrize("seed", SEEDS)
def test_inferred_type_is_the_runtime_type(seed: int) -> None:
    rng = seeded(seed)
    decoy = rng.choice(DECOYS) if seed % 2 else None
    case = direct(rng) if seed % 5 == 0 else build(rng, decoy=decoy)
    found = shown(infer_case(case.source))
    # an evident type is the exact type of the evaluated expression, and a decoy does not hide it
    assert found == runtime_shown(run(case.source)), case.source
    assert found == case.expected, case.source


@pytest.mark.parametrize("seed", SEEDS)
def test_unknown_binding_forms_are_never_reported(seed: int) -> None:
    rng = seeded(seed)
    poison = POISONS[seed % len(POISONS)]
    case = build(rng, poison=poison, decoy=rng.choice(DECOYS) if seed % 2 else None)
    assert infer_case(case.source) is None, case.source


@pytest.mark.parametrize("seed", range(40))
def test_unknown_expressions_are_never_reported(seed: int) -> None:
    case = unknown_expression(seeded(seed))
    assert infer_case(case.source) is None, case.source

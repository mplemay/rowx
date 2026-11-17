from rowx_core import FieldPlan, ShapeSpec
from rowx_core._shape import native_shape


class FakeShape:
    def __init__(  # ruff: ignore[too-many-arguments]
        self,
        ctor: type,
        form: str,
        matching: str,
        type_name: str,
        fields: list[tuple[str, tuple[str, ...], bool, int]],
        *,
        validate: bool,
    ) -> None:
        self.args = (ctor, form, matching, type_name, fields, validate)


def make_spec() -> ShapeSpec:
    class Row:
        id: int

    return ShapeSpec(
        ctor=Row,
        form="keyword",
        matching="by_name",
        type_name="Row",
        fields=(FieldPlan(key="id", names=("id",), nullable=True, flags=2, required=True),),
    )


def test_native_shape_builds_once_and_caches_per_class() -> None:
    spec = make_spec()
    first = native_shape(spec, lambda: FakeShape)
    assert isinstance(first, FakeShape)
    assert first.args[1:] == ("keyword", "by_name", "Row", [("id", ("id",), True, 3)], True)
    assert native_shape(spec, lambda: FakeShape) is first
    assert spec.native[FakeShape] is first


def test_native_shape_looks_the_class_up_on_every_call() -> None:
    spec = make_spec()
    current = [FakeShape]
    first = native_shape(spec, lambda: current[0])

    class Replacement(FakeShape):
        pass

    current[0] = Replacement
    second = native_shape(spec, lambda: current[0])
    assert isinstance(second, Replacement)
    assert second is not first
    # a second class does not evict the first
    assert spec.native[FakeShape] is first
    assert spec.native[Replacement] is second


def test_native_shape_passes_a_native_shape_and_none_through() -> None:
    built = FakeShape(object, "keyword", "by_name", "Row", [], validate=True)

    def unreachable() -> type[FakeShape]:
        raise AssertionError

    assert native_shape(built, unreachable) is built
    assert native_shape(None, unreachable) is None

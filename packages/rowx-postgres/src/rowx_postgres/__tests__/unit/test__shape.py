from rowx_core import FieldPlan, ShapeSpec
from rowx_postgres._core import RowShape
from rowx_postgres._shape import native_shape


def test_native_shape_is_cached_per_backend() -> None:
    class Row:
        id: int

    spec = ShapeSpec(
        ctor=Row,
        form="keyword",
        matching="by_name",
        type_name="Row",
        fields=(FieldPlan(key="id", names=("id",), nullable=False, flags=0, required=True),),
    )
    first = native_shape(spec)
    second = native_shape(spec)
    assert isinstance(first, RowShape)
    assert first is second
    other = object()
    spec.native[other] = other
    assert native_shape(spec) is first
    assert spec.native[other] is other
    assert spec.native[RowShape] is first
    assert native_shape(first) is first
    assert native_shape(None) is None

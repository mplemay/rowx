from typing import TYPE_CHECKING, cast

from rowx_core._rowplan import ShapeSpec

if TYPE_CHECKING:
    from collections.abc import Callable


def native_shape[N](spec: ShapeSpec | N | None, native: Callable[[], Callable[..., N]], /) -> N | None:
    if not isinstance(spec, ShapeSpec):
        return spec
    # the class is fetched through the getter on each call so a test can replace it on the backend module
    row_shape = native()
    if (cached := spec.native.get(row_shape)) is not None:
        return cast("N", cached)
    built = row_shape(
        spec.ctor,
        spec.form,
        spec.matching,
        spec.type_name,
        [(item.key, item.names, item.required, int(item.nullable) | item.flags) for item in spec.fields],
        validate=spec.validate,
    )
    spec.native[row_shape] = built
    return built

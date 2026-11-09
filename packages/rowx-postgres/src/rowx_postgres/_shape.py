from typing import TYPE_CHECKING, cast

from rowx_core import ShapeSpec

import rowx_postgres._core as postgres_core

if TYPE_CHECKING:
    from rowx_postgres._core import RowShape


def native_shape(spec: ShapeSpec | RowShape | None) -> RowShape | None:
    if not isinstance(spec, ShapeSpec):
        return spec
    # looked up each call so a test can replace the constructor on the module
    row_shape = postgres_core.RowShape
    if (cached := spec.native.get(row_shape)) is not None:
        return cast("RowShape", cached)
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

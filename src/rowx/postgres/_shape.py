from typing import TYPE_CHECKING, cast

import rowx.postgres._core as postgres_core

if TYPE_CHECKING:
    from rowx._rowplan import ShapeSpec
    from rowx.postgres._core import RowShape


def native_shape(spec: ShapeSpec | None) -> RowShape | None:
    if spec is None:
        return None
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

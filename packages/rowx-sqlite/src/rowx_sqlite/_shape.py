from typing import TYPE_CHECKING

from rowx_core._shape import native_shape as native_shape_impl  # ruff: ignore[import-private-name]

import rowx_sqlite._core as sqlite_core

if TYPE_CHECKING:
    from rowx_core import ShapeSpec

    from rowx_sqlite._core import RowShape


def row_shape_class() -> type[RowShape]:
    return sqlite_core.RowShape


def native_shape(spec: ShapeSpec | RowShape | None) -> RowShape | None:
    # the getter reads the attribute at call time, so a test can replace the constructor on the module
    return native_shape_impl(spec, row_shape_class)

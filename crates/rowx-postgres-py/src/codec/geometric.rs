use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyTuple, PyType};
use rowx_postgres::Type;
use rowx_postgres::exec::Param;
use rowx_postgres::wire::geometric::{self, Kind, Point, Shape};

use crate::codec::extract::PyParam;
use crate::errors::decode_error;

const KINDS: [Kind; 7] = [
    Kind::Point,
    Kind::Line,
    Kind::LineSegment,
    Kind::Box,
    Kind::Path,
    Kind::Polygon,
    Kind::Circle,
];

static CLASSES: PyOnceLock<Vec<Py<PyType>>> = PyOnceLock::new();

/// The Python class name of a kind (the `rowx.postgres` dataclass).
#[must_use]
pub const fn class_name(kind: Kind) -> &'static str {
    match kind {
        Kind::Point => "Point",
        Kind::Line => "Line",
        Kind::LineSegment => "LineSegment",
        Kind::Box => "Box",
        Kind::Path => "Path",
        Kind::Polygon => "Polygon",
        Kind::Circle => "Circle",
    }
}

/// The geometric kind of a Postgres type, if it is one.
#[must_use]
pub const fn kind_of(ty: &Type) -> Option<Kind> {
    match *ty {
        Type::POINT => Some(Kind::Point),
        Type::LINE => Some(Kind::Line),
        Type::LSEG => Some(Kind::LineSegment),
        Type::BOX => Some(Kind::Box),
        Type::PATH => Some(Kind::Path),
        Type::POLYGON => Some(Kind::Polygon),
        Type::CIRCLE => Some(Kind::Circle),
        _ => None,
    }
}

/// The `rowx.postgres` classes, in the order of `KINDS`.
///
/// # Errors
/// An import failure of `rowx.postgres`.
pub fn classes(py: Python<'_>) -> PyResult<&[Py<PyType>]> {
    CLASSES
        .get_or_try_init(py, || {
            let module = py.import("rowx.postgres")?;
            KINDS
                .iter()
                .map(|kind| {
                    Ok(module
                        .getattr(class_name(*kind))?
                        .cast_into::<PyType>()?
                        .unbind())
                })
                .collect::<PyResult<Vec<_>>>()
        })
        .map(Vec::as_slice)
}

/// # Errors
/// An import failure of `rowx.postgres`.
pub fn class(py: Python<'_>, kind: Kind) -> PyResult<Bound<'_, PyType>> {
    let index = KINDS.iter().position(|k| *k == kind).unwrap_or_default();
    classes(py)?
        .get(index)
        .map(|cls| cls.bind(py).clone())
        .ok_or_else(|| decode_error(py, "the geometric classes are missing"))
}

/// The geometric kind of a Python object, if it is an instance of one of the dataclasses.
///
/// # Errors
/// An import failure of `rowx.postgres`.
pub fn kind_of_object(py: Python<'_>, obj: &Bound<'_, PyAny>) -> PyResult<Option<Kind>> {
    for (kind, cls) in KINDS.iter().zip(classes(py)?) {
        if obj.is_instance(cls.bind(py))? {
            return Ok(Some(*kind));
        }
    }
    Ok(None)
}

/// # Errors
/// An import failure of `rowx.postgres`.
pub fn point_class(py: Python<'_>) -> PyResult<Bound<'_, PyType>> {
    class(py, Kind::Point)
}

fn point(py: Python<'_>, point: Point) -> PyResult<Bound<'_, PyAny>> {
    class(py, Kind::Point)?.call1((point.x, point.y))
}

fn points<'py>(py: Python<'py>, items: &[Point]) -> PyResult<Bound<'py, PyTuple>> {
    let built = items
        .iter()
        .map(|p| point(py, *p))
        .collect::<PyResult<Vec<_>>>()?;
    PyTuple::new(py, built)
}

/// # Errors
/// `DecodeError` for a value whose length does not fit the type.
pub fn decode<'py>(py: Python<'py>, ty: &Type, raw: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    let Some(kind) = kind_of(ty) else {
        return Err(decode_error(
            py,
            &format!("{} is not a geometric type", ty.name()),
        ));
    };
    let shape = geometric::decode(kind, raw).map_err(|e| decode_error(py, &e.to_string()))?;
    let cls = class(py, kind)?;
    match shape {
        Shape::Point(p) => point(py, p),
        Shape::Line { a, b, c } => cls.call1((a, b, c)),
        Shape::LineSegment(start, end) => cls.call1((point(py, start)?, point(py, end)?)),
        Shape::Box {
            upper_right,
            lower_left,
        } => cls.call1((point(py, upper_right)?, point(py, lower_left)?)),
        Shape::Path {
            points: items,
            closed,
        } => cls.call1((points(py, &items)?, closed)),
        Shape::Polygon(items) => cls.call1((points(py, &items)?,)),
        Shape::Circle { center, radius } => cls.call1((center.x, center.y, radius)),
    }
}

/// # Errors
/// A description of why the value cannot be encoded as the geometric type.
pub fn encode(ty: &Type, param: &PyParam) -> Result<Param, String> {
    match (kind_of(ty), param) {
        (Some(kind), PyParam::Geometric(shape)) if shape.kind() == kind => geometric::encode(shape)
            .map(Param::binary)
            .map_err(|e| e.to_string()),
        _ => Err(format!(
            "a Python {} cannot be encoded as {}",
            param.type_name(),
            ty.name()
        )),
    }
}

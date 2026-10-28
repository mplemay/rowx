use thiserror::Error;

const F64_LEN: usize = 8;
const POINT_LEN: usize = 2 * F64_LEN;

#[derive(Debug, Error, PartialEq, Eq)]
pub enum GeometricError {
    #[error("a {0} value is too short")]
    Short(&'static str),
    #[error("a {name} value has {extra} trailing bytes")]
    Trailing { name: &'static str, extra: usize },
    #[error("a {name} needs at least one point, got a count of {count}")]
    Points { name: &'static str, count: i64 },
    #[error("a {name} of {count} points needs {expected} data bytes, got {got}")]
    Data {
        name: &'static str,
        count: usize,
        expected: usize,
        got: usize,
    },
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Point {
    pub x: f64,
    pub y: f64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Point,
    Line,
    LineSegment,
    Box,
    Path,
    Polygon,
    Circle,
}

impl Kind {
    #[must_use]
    pub const fn name(self) -> &'static str {
        match self {
            Self::Point => "point",
            Self::Line => "line",
            Self::LineSegment => "lseg",
            Self::Box => "box",
            Self::Path => "path",
            Self::Polygon => "polygon",
            Self::Circle => "circle",
        }
    }
}

/// A geometric value in the order the server sends it.
#[derive(Debug, Clone, PartialEq)]
pub enum Shape {
    Point(Point),
    // the coefficients of `Ax + By + C = 0`
    Line {
        a: f64,
        b: f64,
        c: f64,
    },
    LineSegment(Point, Point),
    // the server canonicalises a box so that the upper right corner comes first
    Box {
        upper_right: Point,
        lower_left: Point,
    },
    Path {
        points: Vec<Point>,
        closed: bool,
    },
    Polygon(Vec<Point>),
    Circle {
        center: Point,
        radius: f64,
    },
}

impl Shape {
    #[must_use]
    pub const fn kind(&self) -> Kind {
        match self {
            Self::Point(_) => Kind::Point,
            Self::Line { .. } => Kind::Line,
            Self::LineSegment(..) => Kind::LineSegment,
            Self::Box { .. } => Kind::Box,
            Self::Path { .. } => Kind::Path,
            Self::Polygon(_) => Kind::Polygon,
            Self::Circle { .. } => Kind::Circle,
        }
    }
}

struct Reader<'a> {
    rest: &'a [u8],
    name: &'static str,
}

impl Reader<'_> {
    fn take<const N: usize>(&mut self) -> Result<[u8; N], GeometricError> {
        let (head, rest) = self
            .rest
            .split_first_chunk::<N>()
            .ok_or(GeometricError::Short(self.name))?;
        self.rest = rest;
        Ok(*head)
    }

    fn float(&mut self) -> Result<f64, GeometricError> {
        self.take::<F64_LEN>().map(f64::from_be_bytes)
    }

    fn point(&mut self) -> Result<Point, GeometricError> {
        Ok(Point {
            x: self.float()?,
            y: self.float()?,
        })
    }

    fn points(&mut self) -> Result<Vec<Point>, GeometricError> {
        let declared = i32::from_be_bytes(self.take::<4>()?);
        let count = usize::try_from(declared)
            .ok()
            .filter(|count| *count > 0)
            .ok_or_else(|| GeometricError::Points {
                name: self.name,
                count: i64::from(declared),
            })?;
        // checked before anything is allocated, so a forged count cannot reserve memory
        let expected = count.saturating_mul(POINT_LEN);
        if self.rest.len() != expected {
            return Err(GeometricError::Data {
                name: self.name,
                count,
                expected,
                got: self.rest.len(),
            });
        }
        (0..count).map(|_| self.point()).collect()
    }

    const fn finish(self) -> Result<(), GeometricError> {
        if self.rest.is_empty() {
            Ok(())
        } else {
            Err(GeometricError::Trailing {
                name: self.name,
                extra: self.rest.len(),
            })
        }
    }
}

/// # Errors
/// `GeometricError` for a value whose length or point count does not fit its type.
pub fn decode(kind: Kind, raw: &[u8]) -> Result<Shape, GeometricError> {
    let mut reader = Reader {
        rest: raw,
        name: kind.name(),
    };
    let shape = match kind {
        Kind::Point => Shape::Point(reader.point()?),
        Kind::Line => Shape::Line {
            a: reader.float()?,
            b: reader.float()?,
            c: reader.float()?,
        },
        Kind::LineSegment => Shape::LineSegment(reader.point()?, reader.point()?),
        Kind::Box => Shape::Box {
            upper_right: reader.point()?,
            lower_left: reader.point()?,
        },
        Kind::Path => {
            let closed = reader.take::<1>()?[0] != 0;
            Shape::Path {
                closed,
                points: reader.points()?,
            }
        }
        Kind::Polygon => Shape::Polygon(reader.points()?),
        Kind::Circle => Shape::Circle {
            center: reader.point()?,
            radius: reader.float()?,
        },
    };
    reader.finish()?;
    Ok(shape)
}

fn put_point(out: &mut Vec<u8>, point: Point) {
    out.extend_from_slice(&point.x.to_be_bytes());
    out.extend_from_slice(&point.y.to_be_bytes());
}

fn put_points(
    out: &mut Vec<u8>,
    name: &'static str,
    points: &[Point],
) -> Result<(), GeometricError> {
    // the server rejects a path or polygon with no points, and a count must fit an i32
    let count = i32::try_from(points.len())
        .ok()
        .filter(|count| *count > 0)
        .ok_or_else(|| GeometricError::Points {
            name,
            count: i64::try_from(points.len()).unwrap_or(i64::MAX),
        })?;
    out.extend_from_slice(&count.to_be_bytes());
    for point in points {
        put_point(out, *point);
    }
    Ok(())
}

/// # Errors
/// `GeometricError::Points` for a `path` or `polygon` with no points, or too many to count in an `i32`.
pub fn encode(shape: &Shape) -> Result<Vec<u8>, GeometricError> {
    let mut out = Vec::with_capacity(POINT_LEN * 2);
    match shape {
        Shape::Point(point) => put_point(&mut out, *point),
        Shape::Line { a, b, c } => {
            for value in [a, b, c] {
                out.extend_from_slice(&value.to_be_bytes());
            }
        }
        Shape::LineSegment(start, end) => {
            put_point(&mut out, *start);
            put_point(&mut out, *end);
        }
        Shape::Box {
            upper_right,
            lower_left,
        } => {
            put_point(&mut out, *upper_right);
            put_point(&mut out, *lower_left);
        }
        Shape::Path { points, closed } => {
            out.push(u8::from(*closed));
            put_points(&mut out, Kind::Path.name(), points)?;
        }
        Shape::Polygon(points) => put_points(&mut out, Kind::Polygon.name(), points)?,
        Shape::Circle { center, radius } => {
            put_point(&mut out, *center);
            out.extend_from_slice(&radius.to_be_bytes());
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use proptest::prelude::*;

    use super::{GeometricError, Kind, Point, Shape, decode, encode};

    const ALL: [Kind; 7] = [
        Kind::Point,
        Kind::Line,
        Kind::LineSegment,
        Kind::Box,
        Kind::Path,
        Kind::Polygon,
        Kind::Circle,
    ];

    fn pt(x: f64, y: f64) -> Point {
        Point { x, y }
    }

    fn floats(values: &[f64]) -> Vec<u8> {
        values.iter().flat_map(|v| v.to_be_bytes()).collect()
    }

    #[test]
    fn point_is_two_big_endian_float8() {
        let raw = floats(&[1.5, -2.0]);
        assert_eq!(decode(Kind::Point, &raw), Ok(Shape::Point(pt(1.5, -2.0))));
        assert_eq!(encode(&Shape::Point(pt(1.5, -2.0))), Ok(raw));
    }

    #[test]
    fn path_has_a_closed_byte_then_a_count() {
        let mut raw = vec![1, 0, 0, 0, 2];
        raw.extend(floats(&[0.0, 1.0, 2.0, 3.0]));
        let shape = Shape::Path {
            points: vec![pt(0.0, 1.0), pt(2.0, 3.0)],
            closed: true,
        };
        assert_eq!(decode(Kind::Path, &raw), Ok(shape.clone()));
        assert_eq!(encode(&shape), Ok(raw));
    }

    #[test]
    fn polygon_has_a_count_then_points() {
        let mut raw = vec![0, 0, 0, 1];
        raw.extend(floats(&[4.0, 5.0]));
        let shape = Shape::Polygon(vec![pt(4.0, 5.0)]);
        assert_eq!(decode(Kind::Polygon, &raw), Ok(shape.clone()));
        assert_eq!(encode(&shape), Ok(raw));
    }

    #[test]
    fn a_one_point_path_is_valid() {
        let shape = Shape::Path {
            points: vec![pt(1.0, 1.0)],
            closed: false,
        };
        assert_eq!(
            decode(Kind::Path, &encode(&shape).unwrap_or_default()),
            Ok(shape)
        );
    }

    #[test]
    fn an_empty_path_or_polygon_is_rejected() {
        let path = Shape::Path {
            points: vec![],
            closed: false,
        };
        assert!(matches!(
            encode(&path),
            Err(GeometricError::Points { count: 0, .. })
        ));
        assert!(matches!(
            encode(&Shape::Polygon(vec![])),
            Err(GeometricError::Points { count: 0, .. })
        ));
        assert!(matches!(
            decode(Kind::Path, &[0, 0, 0, 0, 0]),
            Err(GeometricError::Points { count: 0, .. })
        ));
        assert!(matches!(
            decode(Kind::Polygon, &[0xff; 4]),
            Err(GeometricError::Points { count: -1, .. })
        ));
    }

    #[test]
    fn a_forged_point_count_reserves_nothing() {
        let raw = [0x7f, 0xff, 0xff, 0xff];
        assert!(matches!(
            decode(Kind::Polygon, &raw),
            Err(GeometricError::Data { got: 0, .. })
        ));
    }

    #[test]
    fn wrong_lengths_are_rejected() {
        for kind in [
            Kind::Point,
            Kind::Line,
            Kind::LineSegment,
            Kind::Box,
            Kind::Circle,
        ] {
            assert_eq!(decode(kind, &[]), Err(GeometricError::Short(kind.name())));
            assert!(matches!(
                decode(kind, &[0; 40]),
                Err(GeometricError::Trailing { .. })
            ));
        }
        assert_eq!(
            decode(Kind::Point, &[0; 17]),
            Err(GeometricError::Trailing {
                name: "point",
                extra: 1
            })
        );
    }

    #[test]
    fn non_finite_coordinates_survive() {
        let shape = Shape::Point(pt(f64::INFINITY, f64::NEG_INFINITY));
        assert_eq!(
            decode(Kind::Point, &encode(&shape).unwrap_or_default()),
            Ok(shape)
        );
        let nan = encode(&Shape::Point(pt(f64::NAN, 0.0))).unwrap_or_default();
        assert!(matches!(
            decode(Kind::Point, &nan),
            Ok(Shape::Point(Point { x, .. })) if x.is_nan()
        ));
    }

    fn coordinate() -> impl Strategy<Value = f64> {
        prop_oneof![
            any::<f64>(),
            Just(f64::NAN),
            Just(f64::INFINITY),
            Just(f64::NEG_INFINITY),
        ]
    }

    fn point() -> impl Strategy<Value = Point> {
        (coordinate(), coordinate()).prop_map(|(x, y)| Point { x, y })
    }

    fn shape() -> impl Strategy<Value = Shape> {
        let points = || proptest::collection::vec(point(), 1..8);
        prop_oneof![
            point().prop_map(Shape::Point),
            (coordinate(), coordinate(), coordinate()).prop_map(|(a, b, c)| Shape::Line {
                a,
                b,
                c
            }),
            (point(), point()).prop_map(|(s, e)| Shape::LineSegment(s, e)),
            (point(), point()).prop_map(|(upper_right, lower_left)| Shape::Box {
                upper_right,
                lower_left
            }),
            (points(), any::<bool>()).prop_map(|(points, closed)| Shape::Path { points, closed }),
            points().prop_map(Shape::Polygon),
            (point(), coordinate()).prop_map(|(center, radius)| Shape::Circle { center, radius }),
        ]
    }

    proptest! {
        // compared as bytes, because NaN is not equal to itself
        #[test]
        fn round_trips(value in shape()) {
            let raw = encode(&value).unwrap_or_default();
            let back = decode(value.kind(), &raw);
            prop_assert!(back.is_ok());
            prop_assert_eq!(back.and_then(|v| encode(&v)), Ok(raw));
        }

        #[test]
        fn decode_never_panics(raw in proptest::collection::vec(any::<u8>(), 0..80)) {
            for kind in ALL {
                let _ = decode(kind, &raw);
            }
        }
    }
}

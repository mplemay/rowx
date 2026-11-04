// a cargo-fuzz target: encoding a value and decoding it again must give the value back
#![no_main]

use libfuzzer_sys::arbitrary::{self, Unstructured};
use libfuzzer_sys::fuzz_target;
use rowx_postgres::wire::geometric::{self, Point, Shape};

fn point(u: &mut Unstructured<'_>) -> arbitrary::Result<Point> {
    Ok(Point { x: u.arbitrary()?, y: u.arbitrary()? })
}

fn points(u: &mut Unstructured<'_>) -> arbitrary::Result<Vec<Point>> {
    let mut out = Vec::new();
    for _ in 0..u.int_in_range(1..=6_usize)? {
        out.push(point(u)?);
    }
    Ok(out)
}

fn check(u: &mut Unstructured<'_>) -> arbitrary::Result<()> {
    let shape = match u.int_in_range(0..=6_u8)? {
        0 => Shape::Point(point(u)?),
        1 => Shape::Line { a: u.arbitrary()?, b: u.arbitrary()?, c: u.arbitrary()? },
        2 => Shape::LineSegment(point(u)?, point(u)?),
        3 => Shape::Box { upper_right: point(u)?, lower_left: point(u)? },
        4 => Shape::Path { points: points(u)?, closed: u.arbitrary()? },
        5 => Shape::Polygon(points(u)?),
        _ => Shape::Circle { center: point(u)?, radius: u.arbitrary()? },
    };
    let kind = shape.kind();
    let raw = match geometric::encode(&shape) {
        Ok(raw) => raw,
        Err(err) => unreachable!("encode failed: {err}"),
    };
    let decoded = geometric::decode(kind, &raw);
    // a NaN is not equal to itself, so the floats are compared as bytes and the structure as a whole
    assert_eq!(decoded.as_ref().map(Shape::kind), Ok(kind));
    assert_eq!(decoded.and_then(|s| geometric::encode(&s)), Ok(raw));
    assert!(geometric::encode(&Shape::Polygon(Vec::new())).is_err());
    assert!(geometric::encode(&Shape::Path { points: Vec::new(), closed: false }).is_err());
    Ok(())
}

fuzz_target!(|data: &[u8]| {
    let _ = check(&mut Unstructured::new(data));
});

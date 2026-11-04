// a cargo-fuzz target: the decoder must return an error for bad input, never panic
#![no_main]

use libfuzzer_sys::fuzz_target;
use rowx_postgres::wire::geometric::{self, Kind};

const KINDS: [Kind; 7] = [
    Kind::Point,
    Kind::Line,
    Kind::LineSegment,
    Kind::Box,
    Kind::Path,
    Kind::Polygon,
    Kind::Circle,
];

fuzz_target!(|data: &[u8]| {
    let Some((&selector, body)) = data.split_first() else {
        return;
    };
    let Some(&kind) = KINDS.get(usize::from(selector) % KINDS.len()) else {
        return;
    };
    if let Ok(shape) = geometric::decode(kind, body) {
        // the bytes are compared, because a NaN payload is not equal to itself and a path flag is any non-zero byte
        assert_eq!(shape.kind(), kind);
        if let Ok(raw) = geometric::encode(&shape) {
            let again = geometric::decode(kind, &raw).and_then(|s| geometric::encode(&s));
            assert_eq!(again, Ok(raw));
        }
    }
});

// a cargo-fuzz target: encoding a value and decoding it again must give the value back
#![no_main]

use libfuzzer_sys::arbitrary::{self, Unstructured};
use libfuzzer_sys::fuzz_target;
use rowx_postgres::wire::cube::{self, Cube};

fn check(u: &mut Unstructured<'_>) -> arbitrary::Result<()> {
    let dim = u.int_in_range(0..=cube::MAX_DIM)?;
    let mut lower_left = Vec::new();
    for _ in 0..dim {
        lower_left.push(u.arbitrary::<f64>()?);
    }
    // a point has equal corners, and a box does not need to
    let mut upper_right = lower_left.clone();
    if u.arbitrary()? {
        for coordinate in &mut upper_right {
            *coordinate = u.arbitrary()?;
        }
    }
    let value = Cube { lower_left, upper_right };
    let raw = match cube::encode(&value) {
        Ok(raw) => raw,
        Err(err) => unreachable!("encode failed: {err}"),
    };
    let decoded = match cube::decode(&raw) {
        Ok(decoded) => decoded,
        Err(err) => unreachable!("decode failed: {err}"),
    };
    // a NaN is not equal to itself, so the coordinates are compared as bits
    let bits = |side: &[f64]| side.iter().map(|c| c.to_bits()).collect::<Vec<_>>();
    assert_eq!(bits(&decoded.lower_left), bits(&value.lower_left));
    assert_eq!(bits(&decoded.upper_right), bits(&value.upper_right));
    Ok(())
}

fuzz_target!(|data: &[u8]| {
    let _ = check(&mut Unstructured::new(data));
});

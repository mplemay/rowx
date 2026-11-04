// a cargo-fuzz target: encoding a value and decoding it again must give the value back
#![no_main]

use libfuzzer_sys::arbitrary::{self, Unstructured};
use libfuzzer_sys::fuzz_target;
use rowx_postgres::wire::range::{self, Bound, Range};

fn bound<'a>(u: &mut Unstructured<'_>, value: &'a [u8]) -> arbitrary::Result<Bound<'a>> {
    Ok(match u.int_in_range(0..=2_u8)? {
        0 => Bound::Unbounded,
        1 => Bound::Inclusive(value),
        _ => Bound::Exclusive(value),
    })
}

fn check(u: &mut Unstructured<'_>) -> arbitrary::Result<()> {
    let low = u.arbitrary::<Vec<u8>>()?;
    let high = u.arbitrary::<Vec<u8>>()?;
    let value = if u.arbitrary()? {
        Range::Empty
    } else {
        Range::Bounds { lower: bound(u, &low)?, upper: bound(u, &high)? }
    };
    let raw = match range::encode(&value) {
        Ok(raw) => raw,
        Err(err) => unreachable!("encode failed: {err}"),
    };
    assert_eq!(range::decode(&raw), Ok(value));
    Ok(())
}

fuzz_target!(|data: &[u8]| {
    let _ = check(&mut Unstructured::new(data));
});

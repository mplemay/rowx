// a cargo-fuzz target: the decoder must return an error for bad input, never panic
#![no_main]

use libfuzzer_sys::arbitrary::Unstructured;
use libfuzzer_sys::fuzz_target;
use rowx_postgres::wire::array;

// a header and dimensions built from the input, where a length of 0 or i32::MAX is likely
fn shaped(data: &[u8]) -> Option<(Vec<u8>, bool)> {
    let mut u = Unstructured::new(data);
    let ndim = u.int_in_range(1..=6_i32).ok()?;
    let mut raw = Vec::new();
    for field in [ndim, 0, 23] {
        raw.extend_from_slice(&field.to_be_bytes());
    }
    let mut zero = false;
    for _ in 0..ndim {
        let len = match u.int_in_range(0..=3_u8).ok()? {
            0 => 0,
            1 => i32::MAX,
            _ => u.int_in_range(0..=4_i32).ok()?,
        };
        zero |= len == 0;
        raw.extend_from_slice(&len.to_be_bytes());
        raw.extend_from_slice(&1_i32.to_be_bytes());
    }
    raw.extend_from_slice(u.take_rest());
    Some((raw, zero))
}

fuzz_target!(|data: &[u8]| {
    // an accepted array must also be walkable end to end
    if let Ok(decoded) = array::decode(data) {
        let _ = decoded.elements.iter().flatten().map(|e| e.len()).sum::<usize>();
    }
    // a zero-length dimension is an empty array when nothing follows the header, and malformed otherwise
    if let Some((raw, zero)) = shaped(data) {
        let decoded = array::decode(&raw);
        if zero {
            if let Ok(array) = decoded {
                assert!(array.dims.is_empty() && array.elements.is_empty());
            }
        }
    }
});

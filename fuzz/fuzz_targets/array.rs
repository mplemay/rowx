// a cargo-fuzz target: the decoder must return an error for bad input, never panic
#![no_main]

use libfuzzer_sys::fuzz_target;
use rowx_postgres::wire::array;

fuzz_target!(|data: &[u8]| {
    // an accepted array must also be walkable end to end
    if let Ok(decoded) = array::decode(data) {
        let _ = decoded.elements.iter().flatten().map(|e| e.len()).sum::<usize>();
    }
});

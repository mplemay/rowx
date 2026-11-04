// a cargo-fuzz target: the decoder must return an error for bad input, never panic
#![no_main]

use libfuzzer_sys::fuzz_target;
use rowx_postgres::wire::cube;

fuzz_target!(|data: &[u8]| {
    if let Ok(decoded) = cube::decode(data) {
        // a box whose corners are equal comes back as a point, so the bytes are compared on the second trip
        if let Ok(raw) = cube::encode(&decoded) {
            let again = cube::decode(&raw).and_then(|c| cube::encode(&c));
            assert_eq!(again, Ok(raw));
        }
    }
});

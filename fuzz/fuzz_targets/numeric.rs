// a cargo-fuzz target: the decoder must return an error for bad input, never panic
#![no_main]

use libfuzzer_sys::fuzz_target;
use rowx_postgres::wire::numeric;

fuzz_target!(|data: &[u8]| {
    if let Ok(value) = numeric::decode(data) {
        let _ = value.to_decimal_string();
        let _ = value.encode();
    }
});

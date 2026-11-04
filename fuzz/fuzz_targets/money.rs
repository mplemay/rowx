// a cargo-fuzz target: the decoder must return an error for bad input, never panic
#![no_main]

use libfuzzer_sys::fuzz_target;
use rowx_postgres::wire::money;

fuzz_target!(|data: &[u8]| {
    if let Ok(minor_units) = money::decode(data) {
        assert_eq!(money::encode(minor_units), data);
    }
});

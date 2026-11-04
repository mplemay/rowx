// a cargo-fuzz target: the decoder must return an error for bad input, never panic
#![no_main]

use libfuzzer_sys::fuzz_target;
use rowx_postgres::wire::ltree;

fuzz_target!(|data: &[u8]| {
    for name in ["ltree", "lquery"] {
        if let Ok(text) = ltree::decode(data, name) {
            assert_eq!(ltree::encode(text), data);
        }
    }
});

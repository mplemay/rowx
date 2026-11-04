// a cargo-fuzz target: the decoder must return an error for bad input, never panic
#![no_main]

use libfuzzer_sys::fuzz_target;
use rowx_postgres::wire::hstore;

fuzz_target!(|data: &[u8]| {
    if let Ok(pairs) = hstore::decode(data) {
        assert_eq!(hstore::encode(&pairs).as_deref(), Ok(data));
    }
});

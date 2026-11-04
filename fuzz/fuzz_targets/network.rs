// a cargo-fuzz target: the decoder must return an error for bad input, never panic
#![no_main]

use libfuzzer_sys::fuzz_target;
use rowx_postgres::wire::network;

fuzz_target!(|data: &[u8]| {
    if let Ok(decoded) = network::decode(data) {
        // the server writes the canonical form, so an accepted value re-encodes to the same bytes
        assert_eq!(network::encode(&decoded).as_deref(), Ok(data));
    }
});

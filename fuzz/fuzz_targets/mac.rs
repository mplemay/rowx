// a cargo-fuzz target: the decoder must return an error for bad input, never panic
#![no_main]

use libfuzzer_sys::fuzz_target;
use rowx_postgres::wire::mac;

fuzz_target!(|data: &[u8]| {
    for width in [mac::MAC_LEN, mac::MAC8_LEN] {
        if let Ok(text) = mac::decode(data, width) {
            assert_eq!(mac::encode(&text, width).as_deref(), Ok(data));
        }
    }
});

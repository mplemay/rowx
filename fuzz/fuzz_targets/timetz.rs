// a cargo-fuzz target: the decoder must return an error for bad input, never panic
#![no_main]

use libfuzzer_sys::fuzz_target;
use rowx_postgres::wire::temporal;

fuzz_target!(|data: &[u8]| {
    let _ = temporal::decode_timetz(data);
});

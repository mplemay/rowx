// a cargo-fuzz target: the decoder must return an error for bad input, never panic
#![no_main]

use libfuzzer_sys::fuzz_target;
use rowx_postgres::dsn;

fuzz_target!(|data: &[u8]| {
    let _ = dsn::parse(&String::from_utf8_lossy(data));
});

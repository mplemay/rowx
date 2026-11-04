// a cargo-fuzz target: the decoder must return an error for bad input, never panic
#![no_main]

use libfuzzer_sys::fuzz_target;
use rowx_postgres::wire::uuid;

fuzz_target!(|data: &[u8]| {
    let text = String::from_utf8_lossy(data);
    if let Ok(bytes) = uuid::parse(&text) {
        let canonical: String = bytes.iter().map(|b| format!("{b:02x}")).collect();
        assert_eq!(uuid::parse(&canonical), Ok(bytes));
    }
});

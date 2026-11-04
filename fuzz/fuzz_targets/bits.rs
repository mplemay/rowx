// a cargo-fuzz target: the decoder must return an error for bad input, never panic
#![no_main]

use libfuzzer_sys::fuzz_target;
use rowx_postgres::wire::bits;

fuzz_target!(|data: &[u8]| {
    if let Ok(text) = bits::decode(data) {
        // the padding bits are dropped, so only the text has to survive another trip
        let encoded = bits::encode(&text);
        assert_eq!(encoded.as_deref().map(bits::decode), Ok(Ok(text)));
    }
});

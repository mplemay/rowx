// a cargo-fuzz target: the decoder must return an error for bad input, never panic
#![no_main]

#[path = "support/elements.rs"]
mod elements;

use libfuzzer_sys::fuzz_target;
use rowx_postgres::wire::range;

fuzz_target!(|data: &[u8]| {
    let Some((&selector, body)) = data.split_first() else {
        return;
    };
    if let Ok(decoded) = range::decode(body) {
        // an accepted range survives a trip through the encoder, and its bounds reach the subtype decoder
        if let Ok(raw) = range::encode(&decoded) {
            assert_eq!(range::decode(&raw), Ok(decoded));
        }
        if let range::Range::Bounds { lower, upper } = decoded {
            for bound in [lower, upper].iter().filter_map(range::Bound::value) {
                elements::decode(selector, bound);
            }
        }
    }
});

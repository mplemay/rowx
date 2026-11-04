// a cargo-fuzz target: an array of any element family must decode or error, and so must every element
#![no_main]

#[path = "support/elements.rs"]
mod elements;

use libfuzzer_sys::fuzz_target;
use rowx_postgres::wire::array;

fuzz_target!(|data: &[u8]| {
    let Some((&selector, body)) = data.split_first() else {
        return;
    };
    if let Ok(decoded) = array::decode(body) {
        for element in decoded.elements.iter().flatten() {
            elements::decode(selector, element);
        }
    }
});

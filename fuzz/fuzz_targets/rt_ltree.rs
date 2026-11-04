// a cargo-fuzz target: encoding a value and decoding it again must give the value back
#![no_main]

use libfuzzer_sys::arbitrary::{self, Unstructured};
use libfuzzer_sys::fuzz_target;
use rowx_postgres::wire::ltree;

fn check(u: &mut Unstructured<'_>) -> arbitrary::Result<()> {
    let text = u.arbitrary::<String>()?;
    let raw = ltree::encode(&text);
    assert_eq!(ltree::decode(&raw, "ltree"), Ok(text.as_str()));
    assert_eq!(ltree::decode(&raw, "lquery"), Ok(text.as_str()));
    Ok(())
}

fuzz_target!(|data: &[u8]| {
    let _ = check(&mut Unstructured::new(data));
});

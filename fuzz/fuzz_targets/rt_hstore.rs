// a cargo-fuzz target: encoding a value and decoding it again must give the value back
#![no_main]

use libfuzzer_sys::arbitrary::{self, Unstructured};
use libfuzzer_sys::fuzz_target;
use rowx_postgres::wire::hstore;

fn check(u: &mut Unstructured<'_>) -> arbitrary::Result<()> {
    let pairs = u.arbitrary::<Vec<(String, Option<String>)>>()?;
    let raw = match hstore::encode(&pairs) {
        Ok(raw) => raw,
        Err(err) => unreachable!("encode failed: {err}"),
    };
    assert_eq!(hstore::decode(&raw), Ok(pairs));
    Ok(())
}

fuzz_target!(|data: &[u8]| {
    let _ = check(&mut Unstructured::new(data));
});

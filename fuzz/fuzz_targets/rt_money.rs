// a cargo-fuzz target: encoding a value and decoding it again must give the value back
#![no_main]

use libfuzzer_sys::arbitrary::{self, Unstructured};
use libfuzzer_sys::fuzz_target;
use rowx_postgres::wire::money;

fn check(u: &mut Unstructured<'_>) -> arbitrary::Result<()> {
    let minor_units = u.arbitrary::<i64>()?;
    assert_eq!(money::decode(&money::encode(minor_units)), Ok(minor_units));
    Ok(())
}

fuzz_target!(|data: &[u8]| {
    let _ = check(&mut Unstructured::new(data));
});

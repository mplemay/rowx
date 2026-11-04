// a cargo-fuzz target: encoding a value and decoding it again must give the value back
#![no_main]

use libfuzzer_sys::arbitrary::{self, Unstructured};
use libfuzzer_sys::fuzz_target;
use rowx_postgres::wire::network::{self, Addr, Network};

fn check(u: &mut Unstructured<'_>) -> arbitrary::Result<()> {
    let addr = if u.arbitrary()? {
        Addr::V4(u.arbitrary()?)
    } else {
        Addr::V6(u.arbitrary()?)
    };
    let bits = u.int_in_range(0..=addr.max_bits())?;
    let value = Network { addr, bits, is_cidr: u.arbitrary()? };
    let raw = match network::encode(&value) {
        Ok(raw) => raw,
        Err(err) => unreachable!("encode failed: {err}"),
    };
    assert_eq!(network::decode(&raw), Ok(value));
    let wide = Network { bits: addr.max_bits() + 1, ..value };
    assert!(network::encode(&wide).is_err());
    Ok(())
}

fuzz_target!(|data: &[u8]| {
    let _ = check(&mut Unstructured::new(data));
});

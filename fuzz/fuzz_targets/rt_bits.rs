// a cargo-fuzz target: encoding a value and decoding it again must give the value back
#![no_main]

use libfuzzer_sys::arbitrary::{self, Unstructured};
use libfuzzer_sys::fuzz_target;
use rowx_postgres::wire::bits;

fn check(u: &mut Unstructured<'_>) -> arbitrary::Result<()> {
    let text: String = u
        .arbitrary::<Vec<bool>>()?
        .into_iter()
        .map(|bit| if bit { '1' } else { '0' })
        .collect();
    let raw = match bits::encode(&text) {
        Ok(raw) => raw,
        Err(err) => unreachable!("encode failed: {err}"),
    };
    assert_eq!(bits::decode(&raw).as_deref(), Ok(text.as_str()));
    // anything but 0 and 1 is refused before it reaches the server
    let stray = u.arbitrary::<String>()?;
    if stray.chars().any(|c| c != '0' && c != '1') {
        assert!(bits::encode(&stray).is_err());
    }
    Ok(())
}

fuzz_target!(|data: &[u8]| {
    let _ = check(&mut Unstructured::new(data));
});

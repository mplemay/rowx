// a cargo-fuzz target: encoding a value and decoding it again must give the value back
#![no_main]

use libfuzzer_sys::arbitrary::{self, Unstructured};
use libfuzzer_sys::fuzz_target;
use rowx_postgres::wire::uuid;

fn check(u: &mut Unstructured<'_>) -> arbitrary::Result<()> {
    let bytes = u.arbitrary::<[u8; 16]>()?;
    // a hyphen may follow any group of four digits except the last, and braces wrap the lot
    let mask = u.arbitrary::<u8>()?;
    let braced = u.arbitrary::<bool>()?;
    let upper = u.arbitrary::<bool>()?;
    let mut text = String::new();
    if braced {
        text.push('{');
    }
    for (index, byte) in bytes.iter().enumerate() {
        text.push_str(&if upper { format!("{byte:02X}") } else { format!("{byte:02x}") });
        if index % 2 == 1 && index < 15 && mask & (1 << (index / 2)) != 0 {
            text.push('-');
        }
    }
    if braced {
        text.push('}');
    }
    assert_eq!(uuid::parse(&text), Ok(bytes));
    Ok(())
}

fuzz_target!(|data: &[u8]| {
    let _ = check(&mut Unstructured::new(data));
});

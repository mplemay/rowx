// a cargo-fuzz target: encoding a value and decoding it again must give the value back
#![no_main]

use libfuzzer_sys::arbitrary::{self, Unstructured};
use libfuzzer_sys::fuzz_target;
use rowx_postgres::wire::mac::{self, MAC8_LEN, MAC_LEN};

fn check(u: &mut Unstructured<'_>) -> arbitrary::Result<()> {
    let wide = u.arbitrary::<bool>()?;
    let width = if wide { MAC8_LEN } else { MAC_LEN };
    let mut bytes = Vec::new();
    for _ in 0..width {
        bytes.push(u.arbitrary::<u8>()?);
    }
    let text = match mac::decode(&bytes, width) {
        Ok(text) => text,
        Err(err) => unreachable!("decode failed: {err}"),
    };
    assert_eq!(mac::encode(&text, width).as_deref(), Ok(bytes.as_slice()));
    // the other spellings the server accepts
    assert_eq!(mac::encode(&text.replace(':', "-"), width).as_deref(), Ok(bytes.as_slice()));
    assert_eq!(mac::encode(&text.replace(':', ""), width).as_deref(), Ok(bytes.as_slice()));
    assert_eq!(mac::encode(&text.to_uppercase(), width).as_deref(), Ok(bytes.as_slice()));
    if wide {
        // six bytes widen with ff:fe in the middle
        let six = match mac::decode(bytes.get(..MAC_LEN).unwrap_or_default(), MAC_LEN) {
            Ok(six) => six,
            Err(err) => unreachable!("decode of the first six bytes failed: {err}"),
        };
        let widened = match mac::encode(&six, MAC8_LEN) {
            Ok(widened) => widened,
            Err(err) => unreachable!("widening to eight bytes failed: {err}"),
        };
        assert_eq!(widened.get(3..5), Some(&[0xff, 0xfe][..]));
    }
    Ok(())
}

fuzz_target!(|data: &[u8]| {
    let _ = check(&mut Unstructured::new(data));
});

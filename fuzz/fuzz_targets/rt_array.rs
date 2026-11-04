// a cargo-fuzz target: encoding a value and decoding it again must give the value back
#![no_main]

use libfuzzer_sys::arbitrary::{self, Unstructured};
use libfuzzer_sys::fuzz_target;
use rowx_postgres::wire::array::{self, Array};

fn check(u: &mut Unstructured<'_>) -> arbitrary::Result<()> {
    let element_oid = u.arbitrary::<u32>()?;
    let ndim = u.int_in_range(0..=6_usize)?;
    let mut dims = Vec::new();
    for _ in 0..ndim {
        dims.push(u.int_in_range(0..=3_usize)?);
    }
    let total = if dims.is_empty() { 0 } else { dims.iter().product() };
    let mut owned = Vec::new();
    for _ in 0..total {
        owned.push(u.arbitrary::<Option<Vec<u8>>>()?);
    }
    let elements: Vec<Option<&[u8]>> = owned.iter().map(Option::as_deref).collect();
    let raw = match array::encode(element_oid, &dims, &elements) {
        Ok(raw) => raw,
        Err(err) => unreachable!("encode failed: {err}"),
    };
    assert_eq!(array::decode(&raw), Ok(Array { element_oid, dims, elements }));
    Ok(())
}

fuzz_target!(|data: &[u8]| {
    let _ = check(&mut Unstructured::new(data));
});

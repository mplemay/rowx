// a cargo-fuzz target: encoding a value and decoding it again must give the value back
#![no_main]

use libfuzzer_sys::arbitrary::{self, Unstructured};
use libfuzzer_sys::fuzz_target;
use rowx_postgres::wire::numeric::{self, DecimalExponent, Numeric};

fn exponent(u: &mut Unstructured<'_>) -> arbitrary::Result<DecimalExponent> {
    Ok(match u.int_in_range(0..=9_u8)? {
        0 => DecimalExponent::Infinity,
        1 => DecimalExponent::NaN,
        2 => DecimalExponent::SignalingNaN,
        // mostly small exponents, now and then one near the limits of the format
        3 => DecimalExponent::Finite(u.arbitrary::<i64>()?),
        _ => DecimalExponent::Finite(i64::from(u.int_in_range(-40..=40_i16)?)),
    })
}

fn check(u: &mut Unstructured<'_>) -> arbitrary::Result<()> {
    // text of any shape never panics the parser, whatever the exponent
    let text = u.arbitrary::<String>()?;
    if let Ok((negative, digits, exp)) = numeric::parse_decimal_str(&text) {
        let _ = Numeric::from_decimal_parts(negative, &digits, exp);
        let _ = numeric::decimal_text(negative, &digits, exp);
    }

    let negative = u.arbitrary::<bool>()?;
    let mut digits = Vec::new();
    for _ in 0..u.int_in_range(0..=24_usize)? {
        digits.push(u.int_in_range(0..=9_u8)?);
    }
    let exp = exponent(u)?;
    let built = Numeric::from_decimal_parts(negative, &digits, exp);

    // the text form parses back to the same value
    if let Ok(spelled) = numeric::decimal_text(negative, &digits, exp) {
        let reparsed = numeric::parse_decimal_str(&spelled)
            .and_then(|(n, d, e)| Numeric::from_decimal_parts(n, &d, e));
        assert_eq!(reparsed, built);
    }

    if let Ok(value) = built {
        let raw = match value.encode() {
            Ok(raw) => raw,
            Err(err) => unreachable!("encode failed: {err}"),
        };
        assert_eq!(numeric::decode(&raw).as_ref(), Ok(&value));
        // and so does the canonical string, which keeps the display scale
        let shown = value.to_decimal_string();
        let reparsed = numeric::parse_decimal_str(&shown)
            .and_then(|(n, d, e)| Numeric::from_decimal_parts(n, &d, e));
        assert_eq!(reparsed, Ok(value));
    }

    // a wire value built from its fields decodes to those fields
    let weight = u.arbitrary::<i16>()?;
    let scale = u.int_in_range(0..=16383_u16)?;
    let mut groups = Vec::new();
    for _ in 0..u.int_in_range(0..=8_usize)? {
        groups.push(u.int_in_range(0..=9999_u16)?);
    }
    let value = Numeric::Finite { negative, weight, scale, digits: groups };
    if let Ok(raw) = value.encode()
        && let Ok(decoded) = numeric::decode(&raw)
    {
        assert_eq!(decoded, value);
        let _ = decoded.to_decimal_string();
    }
    Ok(())
}

fuzz_target!(|data: &[u8]| {
    let _ = check(&mut Unstructured::new(data));
});

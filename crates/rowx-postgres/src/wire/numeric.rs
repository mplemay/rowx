use std::fmt::Write;

use thiserror::Error;

const SIGN_POSITIVE: u16 = 0x0000;
const SIGN_NEGATIVE: u16 = 0x4000;
const SIGN_NAN: u16 = 0xC000;
const SIGN_POS_INF: u16 = 0xD000;
const SIGN_NEG_INF: u16 = 0xF000;
const MAX_SCALE: u16 = 16383;
const HEADER_LEN: usize = 8;
const BASE: u16 = 10_000;

#[derive(Debug, Error, PartialEq, Eq)]
pub enum NumericError {
    #[error("numeric needs at least {HEADER_LEN} header bytes, got {0}")]
    Truncated(usize),
    #[error("numeric declares {declared} digit groups but carries {actual} bytes of digits")]
    LengthMismatch { declared: u16, actual: usize },
    #[error("numeric has an invalid sign word {0:#06x}")]
    Sign(u16),
    #[error("numeric has an invalid scale {0}")]
    Scale(u16),
    #[error("numeric has a digit group {0} outside 0..10000")]
    Digit(u16),
    #[error("a Decimal sNaN cannot be stored in numeric")]
    SignalingNan,
    #[error("numeric has {0} digit groups, more than the wire format can count")]
    TooManyDigits(usize),
    #[error("the Decimal text is malformed")]
    DecimalSyntax,
    #[error("a Decimal digit {0} is outside 0..=9")]
    DecimalDigit(u8),
    #[error("a Decimal with scale {0} exceeds the numeric maximum of {MAX_SCALE}")]
    ScaleRange(u64),
    #[error("the Decimal magnitude is outside the range of numeric")]
    Magnitude,
}

/// The exponent slot of `Decimal.as_tuple()`, which is an int for a finite value and a marker otherwise.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DecimalExponent {
    Finite(i64),
    Infinity,
    NaN,
    SignalingNaN,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Numeric {
    NaN,
    PosInfinity,
    NegInfinity,
    Finite {
        negative: bool,
        // weight of the first base-10000 digit group
        weight: i16,
        scale: u16,
        digits: Vec<u16>,
    },
}

fn be_u16(raw: &[u8], at: usize) -> Option<u16> {
    raw.get(at..at + 2)
        .and_then(|b| <[u8; 2]>::try_from(b).ok())
        .map(u16::from_be_bytes)
}

fn be_i16(raw: &[u8], at: usize) -> Option<i16> {
    be_u16(raw, at).map(|v| i16::from_be_bytes(v.to_be_bytes()))
}

/// # Errors
/// `NumericError` when the bytes are not a well-formed binary numeric.
pub fn decode(raw: &[u8]) -> Result<Numeric, NumericError> {
    // the digit count is unsigned: up to 131072 integer digits need more than i16::MAX groups
    let (Some(ndigits), Some(weight), Some(sign), Some(scale)) = (
        be_u16(raw, 0),
        be_i16(raw, 2),
        be_u16(raw, 4),
        be_u16(raw, 6),
    ) else {
        return Err(NumericError::Truncated(raw.len()));
    };
    let body = &raw[HEADER_LEN..];
    if body.len() != usize::from(ndigits) * 2 {
        return Err(NumericError::LengthMismatch {
            declared: ndigits,
            actual: body.len(),
        });
    }
    match sign {
        SIGN_NAN => return Ok(Numeric::NaN),
        SIGN_POS_INF => return Ok(Numeric::PosInfinity),
        SIGN_NEG_INF => return Ok(Numeric::NegInfinity),
        SIGN_POSITIVE | SIGN_NEGATIVE => {}
        other => return Err(NumericError::Sign(other)),
    }
    if scale > MAX_SCALE {
        return Err(NumericError::Scale(scale));
    }
    let digits = body
        .as_chunks::<2>()
        .0
        .iter()
        .map(|c| u16::from_be_bytes(*c))
        .map(|d| {
            if d < BASE {
                Ok(d)
            } else {
                Err(NumericError::Digit(d))
            }
        })
        .collect::<Result<Vec<_>, _>>()?;
    Ok(Numeric::Finite {
        negative: sign == SIGN_NEGATIVE,
        weight,
        scale,
        digits,
    })
}

// four decimal digits of a base-10000 group, zero padded
fn padded(group: u16) -> impl Iterator<Item = char> {
    format!("{group:04}")
        .chars()
        .collect::<Vec<_>>()
        .into_iter()
}

impl Numeric {
    /// Builds the value `Decimal.as_tuple()` describes, shaped as `numeric_send` would emit the same value:
    /// no leading or trailing zero groups, weight 0 and no groups for zero, and a positive zero.
    ///
    /// # Errors
    /// `DecimalDigit` for a digit above 9, `SignalingNan` for an sNaN, `ScaleRange` for a scale above the
    /// numeric maximum, `Magnitude` past the `i16` weight and `TooManyDigits` past the 16-bit group count.
    pub fn from_decimal_parts(
        negative: bool,
        digits: &[u8],
        exponent: DecimalExponent,
    ) -> Result<Self, NumericError> {
        if let Some(&bad) = digits.iter().find(|&&d| d > 9) {
            return Err(NumericError::DecimalDigit(bad));
        }
        let exponent = match exponent {
            DecimalExponent::NaN => return Ok(Self::NaN),
            DecimalExponent::SignalingNaN => return Err(NumericError::SignalingNan),
            DecimalExponent::Infinity if negative => return Ok(Self::NegInfinity),
            DecimalExponent::Infinity => return Ok(Self::PosInfinity),
            DecimalExponent::Finite(exponent) => exponent,
        };
        let scale = if exponent < 0 {
            u16::try_from(exponent.unsigned_abs())
                .ok()
                .filter(|scale| *scale <= MAX_SCALE)
                .ok_or_else(|| NumericError::ScaleRange(exponent.unsigned_abs()))?
        } else {
            0
        };
        let (Some(first), Some(last)) = (
            digits.iter().position(|&d| d != 0),
            digits.iter().rposition(|&d| d != 0),
        ) else {
            return Ok(Self::Finite {
                negative: false,
                weight: 0,
                scale,
                digits: Vec::new(),
            });
        };
        let significant = digits.get(first..=last).ok_or(NumericError::Magnitude)?;
        let to_i64 = |n: usize| i64::try_from(n).map_err(|_| NumericError::Magnitude);
        // decimal exponents of the last and the first significant digit
        let low = exponent
            .checked_add(to_i64(digits.len() - 1 - last)?)
            .ok_or(NumericError::Magnitude)?;
        let high = low
            .checked_add(to_i64(significant.len() - 1)?)
            .ok_or(NumericError::Magnitude)?;
        // base-10000 group `k` holds the decimal exponents `4k..=4k+3`
        let top = high.div_euclid(4);
        let bottom = low.div_euclid(4);
        let weight = i16::try_from(top).map_err(|_| NumericError::Magnitude)?;
        let count = top
            .checked_sub(bottom)
            .and_then(|span| span.checked_add(1))
            .ok_or(NumericError::Magnitude)?;
        if usize::try_from(count).map_or(true, |count| count > usize::from(u16::MAX)) {
            return Err(NumericError::TooManyDigits(
                usize::try_from(count).unwrap_or(usize::MAX),
            ));
        }
        let digit_at = |exponent: i64| -> u16 {
            high.checked_sub(exponent)
                .and_then(|index| usize::try_from(index).ok())
                .and_then(|index| significant.get(index))
                .map_or(0, |&d| u16::from(d))
        };
        let groups = (bottom..=top)
            .rev()
            .map(|k| {
                (0..4).fold(0u16, |acc, offset| {
                    // most significant decimal place of the group first
                    let place = k * 4 + (3 - offset);
                    acc * 10 + digit_at(place)
                })
            })
            .collect();
        Ok(Self::Finite {
            negative,
            weight,
            scale,
            digits: groups,
        })
    }

    /// The binary wire form, as `numeric_send` produces it.
    ///
    /// # Errors
    /// `TooManyDigits` when the digit groups do not fit the unsigned 16-bit count.
    pub fn encode(&self) -> Result<Vec<u8>, NumericError> {
        let (sign, weight, scale, digits): (u16, i16, u16, &[u16]) = match self {
            Self::NaN => (SIGN_NAN, 0, 0, &[]),
            Self::PosInfinity => (SIGN_POS_INF, 0, 0, &[]),
            Self::NegInfinity => (SIGN_NEG_INF, 0, 0, &[]),
            Self::Finite {
                negative,
                weight,
                scale,
                digits,
            } => (
                if *negative {
                    SIGN_NEGATIVE
                } else {
                    SIGN_POSITIVE
                },
                *weight,
                *scale,
                digits,
            ),
        };
        let count =
            u16::try_from(digits.len()).map_err(|_| NumericError::TooManyDigits(digits.len()))?;
        let mut out = Vec::with_capacity(HEADER_LEN + digits.len() * 2);
        out.extend_from_slice(&count.to_be_bytes());
        out.extend_from_slice(&weight.to_be_bytes());
        out.extend_from_slice(&sign.to_be_bytes());
        out.extend_from_slice(&scale.to_be_bytes());
        for digit in digits {
            out.extend_from_slice(&digit.to_be_bytes());
        }
        Ok(out)
    }

    /// The value as text that `decimal.Decimal` parses, keeping the display scale.
    #[must_use]
    pub fn to_decimal_string(&self) -> String {
        let (negative, weight, scale, digits) = match self {
            Self::NaN => return "NaN".to_owned(),
            Self::PosInfinity => return "Infinity".to_owned(),
            Self::NegInfinity => return "-Infinity".to_owned(),
            Self::Finite {
                negative,
                weight,
                scale,
                digits,
            } => (*negative, i32::from(*weight), usize::from(*scale), digits),
        };
        // group `index` counts from the first stored group, which has weight `weight`
        let group = |index: i32| -> u16 {
            usize::try_from(index)
                .ok()
                .and_then(|i| digits.get(i))
                .copied()
                .unwrap_or(0)
        };
        let mut int_part = String::new();
        for index in 0..=weight {
            int_part.extend(padded(group(index)));
        }
        let int_part = int_part.trim_start_matches('0');
        let mut out = String::new();
        if negative {
            out.push('-');
        }
        out.push_str(if int_part.is_empty() { "0" } else { int_part });
        if scale > 0 {
            let mut frac = String::new();
            let mut index = weight + 1;
            while frac.len() < scale {
                frac.extend(padded(group(index)));
                index += 1;
            }
            frac.truncate(scale);
            out.push('.');
            out.push_str(&frac);
        }
        out
    }
}

/// Reads the canonical `str(Decimal)` form (`-1.50`, `1E+3`, `-Infinity`, `NaN12`, `sNaN`) into the parts
/// `Decimal.as_tuple()` would report. Every digit is validated, so the result never depends on trust.
///
/// # Errors
/// `DecimalSyntax` for anything that is not that form.
pub fn parse_decimal_str(text: &str) -> Result<(bool, Vec<u8>, DecimalExponent), NumericError> {
    let (negative, rest) = text
        .strip_prefix('-')
        .map_or((false, text), |rest| (true, rest));
    if rest == "Infinity" {
        return Ok((negative, vec![0], DecimalExponent::Infinity));
    }
    let payload = |prefix: &str| {
        rest.strip_prefix(prefix)
            .filter(|tail| tail.bytes().all(|b| b.is_ascii_digit()))
    };
    if payload("NaN").is_some() {
        return Ok((negative, Vec::new(), DecimalExponent::NaN));
    }
    if payload("sNaN").is_some() {
        return Ok((negative, Vec::new(), DecimalExponent::SignalingNaN));
    }
    let (mantissa, exponent) = match rest.split_once(['E', 'e']) {
        Some((mantissa, exponent)) => (
            mantissa,
            exponent
                .parse::<i64>()
                .map_err(|_| NumericError::DecimalSyntax)?,
        ),
        None => (rest, 0),
    };
    let (whole, fraction) = mantissa.split_once('.').unwrap_or((mantissa, ""));
    let mut digits = Vec::with_capacity(whole.len() + fraction.len());
    for byte in whole.bytes().chain(fraction.bytes()) {
        if !byte.is_ascii_digit() {
            return Err(NumericError::DecimalSyntax);
        }
        digits.push(byte - b'0');
    }
    if digits.is_empty() {
        return Err(NumericError::DecimalSyntax);
    }
    let scale = i64::try_from(fraction.len()).map_err(|_| NumericError::Magnitude)?;
    let exponent = exponent.checked_sub(scale).ok_or(NumericError::Magnitude)?;
    Ok((negative, digits, DecimalExponent::Finite(exponent)))
}

/// The text Postgres reads for a scalar parameter: the digits with an exponent, as `str(Decimal)` spells
/// it. Every NaN is `NaN`, so the server alone decides whether a magnitude is too large.
///
/// # Errors
/// `SignalingNan` for an sNaN, `DecimalDigit` for a digit above 9.
pub fn decimal_text(
    negative: bool,
    digits: &[u8],
    exponent: DecimalExponent,
) -> Result<String, NumericError> {
    if let Some(&bad) = digits.iter().find(|&&d| d > 9) {
        return Err(NumericError::DecimalDigit(bad));
    }
    let sign = if negative { "-" } else { "" };
    let exponent = match exponent {
        DecimalExponent::NaN => return Ok("NaN".to_owned()),
        DecimalExponent::SignalingNaN => return Err(NumericError::SignalingNan),
        DecimalExponent::Infinity => return Ok(format!("{sign}Infinity")),
        DecimalExponent::Finite(exponent) => exponent,
    };
    let mut out = String::from(sign);
    if digits.is_empty() {
        out.push('0');
    }
    out.extend(digits.iter().map(|d| char::from(b'0' + d)));
    let _ = write!(out, "E{exponent}");
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn wire(weight: i16, sign: u16, scale: u16, digits: &[u16]) -> Vec<u8> {
        let mut out = Vec::new();
        out.extend_from_slice(&u16::try_from(digits.len()).unwrap_or(0).to_be_bytes());
        out.extend_from_slice(&weight.to_be_bytes());
        out.extend_from_slice(&sign.to_be_bytes());
        out.extend_from_slice(&scale.to_be_bytes());
        for d in digits {
            out.extend_from_slice(&d.to_be_bytes());
        }
        out
    }

    fn text(raw: &[u8]) -> String {
        decode(raw)
            .map(|n| n.to_decimal_string())
            .unwrap_or_default()
    }

    #[test]
    fn small_exponent() {
        // 1e-30 is the digit group 100 at weight -8
        assert_eq!(
            text(&wire(-8, SIGN_POSITIVE, 30, &[100])),
            "0.000000000000000000000000000001"
        );
    }

    #[test]
    fn large_exponent() {
        let mut expected = String::from("1");
        expected.push_str(&"0".repeat(100));
        // 1e100 is the digit group 1 at weight 25
        assert_eq!(text(&wire(25, SIGN_POSITIVE, 0, &[1])), expected);
    }

    #[test]
    fn nan_and_infinities() {
        assert_eq!(text(&wire(0, SIGN_NAN, 0, &[])), "NaN");
        assert_eq!(text(&wire(0, SIGN_POS_INF, 0, &[])), "Infinity");
        assert_eq!(text(&wire(0, SIGN_NEG_INF, 0, &[])), "-Infinity");
    }

    #[test]
    fn negative_zero_keeps_scale() {
        assert_eq!(text(&wire(0, SIGN_NEGATIVE, 2, &[])), "-0.00");
    }

    #[test]
    fn trailing_zeros_follow_the_scale() {
        // 12345.6700
        assert_eq!(
            text(&wire(1, SIGN_POSITIVE, 4, &[1, 2345, 6700])),
            "12345.6700"
        );
        // 100 stored as one group at weight 0 would be 100; 10000 needs weight 1
        assert_eq!(text(&wire(1, SIGN_POSITIVE, 0, &[1])), "10000");
        assert_eq!(text(&wire(0, SIGN_POSITIVE, 3, &[1, 5000])), "1.500");
    }

    #[test]
    fn fraction_below_the_first_group() {
        // 0.0001234
        assert_eq!(text(&wire(-1, SIGN_POSITIVE, 7, &[1, 2340])), "0.0001234");
    }

    #[test]
    fn rejects_malformed_input() {
        assert_eq!(decode(&[0; 3]), Err(NumericError::Truncated(3)));
        assert!(matches!(
            decode(&wire(0, 0x1234, 0, &[])),
            Err(NumericError::Sign(0x1234))
        ));
        assert!(matches!(
            decode(&wire(0, SIGN_POSITIVE, 0, &[10_000])),
            Err(NumericError::Digit(10_000))
        ));
        assert!(matches!(
            decode(&wire(0, SIGN_POSITIVE, 20_000, &[])),
            Err(NumericError::Scale(20_000))
        ));
        let mut short = wire(0, SIGN_POSITIVE, 0, &[1]);
        short.pop();
        assert!(matches!(
            decode(&short),
            Err(NumericError::LengthMismatch { .. })
        ));
    }

    #[test]
    fn digit_count_is_unsigned() {
        // 32768 groups overflow i16; postgres reads the count as uint16
        let digits = vec![1; 32_768];
        let decoded = decode(&wire(32_767, SIGN_POSITIVE, 0, &digits));
        assert!(matches!(decoded, Ok(Numeric::Finite { digits, .. }) if digits.len() == 32_768));
    }
}

#[cfg(test)]
mod proptests {
    use super::*;
    use proptest::prelude::*;

    fn finite() -> impl Strategy<Value = Numeric> {
        (
            any::<bool>(),
            prop_oneof![Just(i16::MIN), Just(i16::MAX), any::<i16>()],
            prop_oneof![Just(0u16), Just(1000), Just(MAX_SCALE), 0..=MAX_SCALE],
            prop::collection::vec(0..BASE, 0..12),
        )
            .prop_map(|(negative, weight, scale, digits)| Numeric::Finite {
                negative,
                weight,
                scale,
                digits,
            })
    }

    fn any_numeric() -> impl Strategy<Value = Numeric> {
        prop_oneof![
            Just(Numeric::NaN),
            Just(Numeric::PosInfinity),
            Just(Numeric::NegInfinity),
            finite(),
        ]
    }

    proptest! {
        #[test]
        fn encode_then_decode_is_the_identity(value in any_numeric()) {
            let raw = value.encode();
            prop_assert!(raw.is_ok());
            prop_assert_eq!(decode(&raw.unwrap_or_default()), Ok(value));
        }

        #[test]
        fn every_strict_prefix_of_an_encoding_is_rejected(value in any_numeric(), cut in 0usize..64) {
            let raw = value.encode().unwrap_or_default();
            let cut = cut % raw.len().max(1);
            prop_assert!(decode(&raw[..cut]).is_err());
        }

        #[test]
        fn decoding_random_bytes_never_panics(raw in prop::collection::vec(any::<u8>(), 0..64)) {
            if let Ok(value) = decode(&raw) {
                // the text form of anything the decoder accepts must also be computable
                let _ = value.to_decimal_string();
            }
        }

        #[test]
        fn random_headers_with_consistent_bodies_never_panic(
            weight in any::<i16>(),
            sign in any::<u16>(),
            scale in any::<u16>(),
            digits in prop::collection::vec(any::<u16>(), 0..8),
        ) {
            let mut raw = Vec::new();
            raw.extend_from_slice(&u16::try_from(digits.len()).unwrap_or(0).to_be_bytes());
            raw.extend_from_slice(&weight.to_be_bytes());
            raw.extend_from_slice(&sign.to_be_bytes());
            raw.extend_from_slice(&scale.to_be_bytes());
            for digit in &digits {
                raw.extend_from_slice(&digit.to_be_bytes());
            }
            if let Ok(value) = decode(&raw) {
                let _ = value.to_decimal_string();
            }
        }

        #[test]
        fn decimal_text_shape_follows_the_scale(
            negative in any::<bool>(),
            weight in -3i16..6,
            scale in 0u16..40,
            digits in prop::collection::vec(0..BASE, 0..6),
        ) {
            let text = Numeric::Finite { negative, weight, scale, digits }.to_decimal_string();
            let frac = text.split_once('.').map_or(0, |(_, frac)| frac.len());
            prop_assert_eq!(frac, usize::from(scale));
            prop_assert_eq!(text.starts_with('-'), negative);
        }
    }

    #[test]
    fn the_extreme_scales_and_weights_decode() {
        for (weight, scale) in [(0, 0), (0, MAX_SCALE), (i16::MIN, 0), (i16::MAX, 0)] {
            let value = Numeric::Finite {
                negative: false,
                weight,
                scale,
                digits: vec![1],
            };
            let raw = value.encode().unwrap_or_default();
            assert_eq!(decode(&raw), Ok(value));
        }
    }

    #[test]
    fn a_scale_past_the_maximum_is_rejected() {
        let value = Numeric::Finite {
            negative: false,
            weight: 0,
            scale: MAX_SCALE + 1,
            digits: vec![],
        };
        let raw = value.encode().unwrap_or_default();
        assert_eq!(decode(&raw), Err(NumericError::Scale(MAX_SCALE + 1)));
    }

    #[test]
    fn too_many_digit_groups_cannot_be_encoded() {
        let value = Numeric::Finite {
            negative: false,
            weight: 0,
            scale: 0,
            digits: vec![0; usize::from(u16::MAX) + 1],
        };
        assert_eq!(
            value.encode(),
            Err(NumericError::TooManyDigits(usize::from(u16::MAX) + 1))
        );
    }
}

#[cfg(test)]
mod decimal_parts_tests {
    use super::*;

    fn parts(negative: bool, digits: &[u8], exponent: i64) -> Result<Numeric, NumericError> {
        Numeric::from_decimal_parts(negative, digits, DecimalExponent::Finite(exponent))
    }

    fn finite(negative: bool, weight: i16, scale: u16, digits: &[u16]) -> Numeric {
        Numeric::Finite {
            negative,
            weight,
            scale,
            digits: digits.to_vec(),
        }
    }

    #[test]
    fn plain_integers_and_fractions() {
        assert_eq!(parts(false, &[1, 2, 3], 0), Ok(finite(false, 0, 0, &[123])));
        assert_eq!(
            parts(true, &[1, 2, 3, 4, 5, 6, 7, 0, 0], -4),
            Ok(finite(true, 1, 4, &[1, 2345, 6700]))
        );
        // 1.500 keeps its display scale, and the stored groups drop the trailing zeros
        assert_eq!(
            parts(false, &[1, 5, 0, 0], -3),
            Ok(finite(false, 0, 3, &[1, 5000]))
        );
        // 0.0001234
        assert_eq!(
            parts(false, &[1, 2, 3, 4], -7),
            Ok(finite(false, -1, 7, &[1, 2340]))
        );
        assert_eq!(parts(false, &[1], -30), Ok(finite(false, -8, 30, &[100])));
    }

    #[test]
    fn leading_zeros_are_dropped() {
        assert_eq!(parts(false, &[0, 0, 7], 0), Ok(finite(false, 0, 0, &[7])));
        assert_eq!(
            parts(false, &[0, 0, 1, 0], -2),
            Ok(finite(false, -1, 2, &[1000]))
        );
    }

    #[test]
    fn trailing_zero_groups_are_not_stored() {
        // 10000 is the single group 1 at weight 1
        assert_eq!(
            parts(false, &[1, 0, 0, 0, 0], 0),
            Ok(finite(false, 1, 0, &[1]))
        );
        assert_eq!(parts(false, &[1], 4), Ok(finite(false, 1, 0, &[1])));
        // interior zero groups are kept
        assert_eq!(
            parts(false, &[1, 0, 0, 0, 0, 0, 0, 0, 0, 1], 0),
            Ok(finite(false, 2, 0, &[10, 0, 1]))
        );
    }

    #[test]
    fn zero_has_no_groups_and_no_sign() {
        assert_eq!(parts(true, &[0], 0), Ok(finite(false, 0, 0, &[])));
        assert_eq!(parts(false, &[0], 5), Ok(finite(false, 0, 0, &[])));
        assert_eq!(parts(true, &[0], -3), Ok(finite(false, 0, 3, &[])));
        assert_eq!(parts(false, &[], 0), Ok(finite(false, 0, 0, &[])));
    }

    #[test]
    fn a_positive_exponent_is_zero_padded_by_the_weight() {
        // 1E+100000 is the single group 1 at weight 25000
        assert_eq!(
            parts(false, &[1], 100_000),
            Ok(finite(false, 25_000, 0, &[1]))
        );
        // 12E+5 is 1200000, groups 120 and 0000 with the last dropped
        assert_eq!(parts(false, &[1, 2], 5), Ok(finite(false, 1, 0, &[120])));
        assert_eq!(parts(false, &[1, 2], 6), Ok(finite(false, 1, 0, &[1200])));
        assert_eq!(
            parts(false, &[1, 2], 7),
            Ok(finite(false, 2, 0, &[1, 2000]))
        );
    }

    #[test]
    fn the_weight_is_bounded_by_i16() {
        // the highest decimal place of weight i16::MAX is 4 * 32767 + 3
        let top = 4 * i64::from(i16::MAX) + 3;
        assert_eq!(
            parts(false, &[1], top),
            Ok(finite(false, i16::MAX, 0, &[1000]))
        );
        assert_eq!(parts(false, &[1], top + 1), Err(NumericError::Magnitude));
        assert_eq!(parts(false, &[1], i64::MAX), Err(NumericError::Magnitude));
        assert_eq!(
            parts(false, &[9, 9], i64::MAX),
            Err(NumericError::Magnitude)
        );
    }

    #[test]
    fn a_scale_above_the_maximum_is_rejected() {
        assert!(parts(false, &[1], -i64::from(MAX_SCALE)).is_ok());
        assert_eq!(
            parts(false, &[1], -i64::from(MAX_SCALE) - 1),
            Err(NumericError::ScaleRange(u64::from(MAX_SCALE) + 1))
        );
        assert_eq!(
            parts(false, &[0], i64::MIN),
            Err(NumericError::ScaleRange(i64::MIN.unsigned_abs()))
        );
    }

    #[test]
    fn more_digits_than_the_weight_allows_are_rejected() {
        // 131073 digits put the first one at weight 32768
        let digits = vec![1u8; 4 * 32_768 + 1];
        assert_eq!(parts(false, &digits, 0), Err(NumericError::Magnitude));
    }

    #[test]
    fn special_values() {
        let build = |negative, exponent| Numeric::from_decimal_parts(negative, &[], exponent);
        assert_eq!(build(false, DecimalExponent::NaN), Ok(Numeric::NaN));
        assert_eq!(build(true, DecimalExponent::NaN), Ok(Numeric::NaN));
        assert_eq!(
            build(false, DecimalExponent::Infinity),
            Ok(Numeric::PosInfinity)
        );
        assert_eq!(
            build(true, DecimalExponent::Infinity),
            Ok(Numeric::NegInfinity)
        );
        assert_eq!(
            build(false, DecimalExponent::SignalingNaN),
            Err(NumericError::SignalingNan)
        );
        // a NaN payload is ignored
        assert_eq!(
            Numeric::from_decimal_parts(false, &[1, 2, 3], DecimalExponent::NaN),
            Ok(Numeric::NaN)
        );
    }

    #[test]
    fn a_digit_above_nine_is_rejected() {
        assert_eq!(
            parts(false, &[1, 10], 0),
            Err(NumericError::DecimalDigit(10))
        );
        assert_eq!(
            parts(false, &[255], 0),
            Err(NumericError::DecimalDigit(255))
        );
        assert_eq!(
            Numeric::from_decimal_parts(false, &[11], DecimalExponent::NaN),
            Err(NumericError::DecimalDigit(11))
        );
    }

    #[test]
    fn the_encoding_decodes_to_the_same_text() {
        let raw = parts(true, &[1, 2, 3, 4, 5], -2).and_then(|n| n.encode());
        assert_eq!(
            raw.and_then(|raw| decode(&raw))
                .map(|n| n.to_decimal_string()),
            Ok("-123.45".to_owned())
        );
    }
}

#[cfg(test)]
mod decimal_text_tests {
    use super::*;

    #[test]
    fn spells_digits_with_an_exponent() {
        let finite = DecimalExponent::Finite;
        assert_eq!(
            decimal_text(false, &[1, 5], finite(-1)),
            Ok("15E-1".to_owned())
        );
        assert_eq!(
            decimal_text(true, &[1], finite(200_000)),
            Ok("-1E200000".to_owned())
        );
        assert_eq!(decimal_text(false, &[], finite(0)), Ok("0E0".to_owned()));
    }

    #[test]
    fn special_values_and_refusals() {
        assert_eq!(
            decimal_text(true, &[1, 2], DecimalExponent::NaN),
            Ok("NaN".to_owned())
        );
        assert_eq!(
            decimal_text(true, &[0], DecimalExponent::Infinity),
            Ok("-Infinity".to_owned())
        );
        assert_eq!(
            decimal_text(false, &[], DecimalExponent::SignalingNaN),
            Err(NumericError::SignalingNan)
        );
        assert_eq!(
            decimal_text(false, &[10], DecimalExponent::Finite(0)),
            Err(NumericError::DecimalDigit(10))
        );
    }
}

#[cfg(test)]
mod decimal_str_tests {
    use super::*;

    fn parsed(text: &str) -> Result<(bool, Vec<u8>, DecimalExponent), NumericError> {
        parse_decimal_str(text)
    }

    #[test]
    fn reads_every_form_str_decimal_produces() {
        let finite = DecimalExponent::Finite;
        assert_eq!(parsed("0"), Ok((false, vec![0], finite(0))));
        assert_eq!(parsed("-1.50"), Ok((true, vec![1, 5, 0], finite(-2))));
        assert_eq!(parsed("1E+3"), Ok((false, vec![1], finite(3))));
        assert_eq!(parsed("1.5E-7"), Ok((false, vec![1, 5], finite(-8))));
        assert_eq!(
            parsed("0.000123"),
            Ok((false, vec![0, 0, 0, 0, 1, 2, 3], finite(-6)))
        );
        assert_eq!(parsed("1E+100000"), Ok((false, vec![1], finite(100_000))));
        // `decimal.getcontext().capitals = 0` prints a lowercase exponent marker
        assert_eq!(parsed("1e+5"), Ok((false, vec![1], finite(5))));
        assert_eq!(parsed("-1.5e-3"), Ok((true, vec![1, 5], finite(-4))));
        assert_eq!(
            parsed("-Infinity"),
            Ok((true, vec![0], DecimalExponent::Infinity))
        );
        assert_eq!(parsed("NaN"), Ok((false, vec![], DecimalExponent::NaN)));
        assert_eq!(parsed("-NaN123"), Ok((true, vec![], DecimalExponent::NaN)));
        assert_eq!(
            parsed("sNaN7"),
            Ok((false, vec![], DecimalExponent::SignalingNaN))
        );
    }

    #[test]
    fn rejects_everything_else() {
        for text in [
            "", "-", "E5", "1E", "1E+x", "1.2.3", "1 ", "+1", "١٢", "nan", "Inf", "NaNx", "e5",
            "1e", "1e+x", ".", "-.",
        ] {
            assert_eq!(parsed(text), Err(NumericError::DecimalSyntax), "{text:?}");
        }
    }

    #[test]
    fn an_exponent_that_cannot_hold_the_scale_is_refused() {
        assert_eq!(
            parsed(&format!("1.5E{}", i64::MIN)),
            Err(NumericError::Magnitude)
        );
    }
}

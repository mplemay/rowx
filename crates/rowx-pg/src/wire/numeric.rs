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
    LengthMismatch { declared: i16, actual: usize },
    #[error("numeric has an invalid sign word {0:#06x}")]
    Sign(u16),
    #[error("numeric has an invalid scale {0}")]
    Scale(u16),
    #[error("numeric has a digit group {0} outside 0..10000")]
    Digit(u16),
    #[error("a Decimal sNaN cannot be stored in numeric")]
    SignalingNan,
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
    let (Some(ndigits), Some(weight), Some(sign), Some(scale)) = (
        be_i16(raw, 0),
        be_i16(raw, 2),
        be_u16(raw, 4),
        be_u16(raw, 6),
    ) else {
        return Err(NumericError::Truncated(raw.len()));
    };
    let body = &raw[HEADER_LEN..];
    let count = usize::try_from(ndigits).unwrap_or(usize::MAX);
    if body.len() != count.saturating_mul(2) {
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
        .chunks_exact(2)
        .map(|c| u16::from_be_bytes([c[0], c[1]]))
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

/// Maps `str(Decimal)` to text Postgres accepts; every NaN becomes `NaN`.
///
/// # Errors
/// `SignalingNan` for `sNaN`.
pub fn normalize_decimal_text(text: &str) -> Result<String, NumericError> {
    let unsigned = text.trim_start_matches(['+', '-']);
    if unsigned.eq_ignore_ascii_case("snan") {
        return Err(NumericError::SignalingNan);
    }
    if unsigned.eq_ignore_ascii_case("nan") {
        return Ok("NaN".to_owned());
    }
    Ok(text.to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn wire(weight: i16, sign: u16, scale: u16, digits: &[u16]) -> Vec<u8> {
        let mut out = Vec::new();
        out.extend_from_slice(&i16::try_from(digits.len()).unwrap_or(0).to_be_bytes());
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
    fn encode_text_normalises_nan() {
        assert_eq!(normalize_decimal_text("-NaN").as_deref(), Ok("NaN"));
        assert_eq!(normalize_decimal_text("1E+2").as_deref(), Ok("1E+2"));
        assert_eq!(
            normalize_decimal_text("-Infinity").as_deref(),
            Ok("-Infinity")
        );
        assert_eq!(
            normalize_decimal_text("sNaN"),
            Err(NumericError::SignalingNan)
        );
    }
}

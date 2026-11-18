use rusqlite::types::{Value, ValueRef};

use super::{DecodeError, mismatch, text};

/// A decimal number as validated text: an optional sign, digits with an optional fraction and an
/// optional exponent. NaN, infinities and surrounding whitespace are not accepted.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Decimal(String);

impl Decimal {
    /// `None` for text that is not a finite decimal number.
    #[must_use]
    pub fn parse(text: &str) -> Option<Self> {
        is_valid(text).then(|| Self(text.to_owned()))
    }

    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

fn digits(bytes: &[u8], from: usize) -> usize {
    bytes.get(from..).map_or(0, |tail| {
        tail.iter().take_while(|byte| byte.is_ascii_digit()).count()
    })
}

fn is_valid(text: &str) -> bool {
    let bytes = text.as_bytes();
    let mut at = usize::from(matches!(bytes.first(), Some(b'+' | b'-')));
    let whole = digits(bytes, at);
    at = at.saturating_add(whole);
    let mut fraction = 0;
    if bytes.get(at) == Some(&b'.') {
        at = at.saturating_add(1);
        fraction = digits(bytes, at);
        at = at.saturating_add(fraction);
    }
    if whole == 0 && fraction == 0 {
        return false;
    }
    if matches!(bytes.get(at), Some(b'e' | b'E')) {
        at = at.saturating_add(1);
        at = at.saturating_add(usize::from(matches!(bytes.get(at), Some(b'+' | b'-'))));
        let exponent = digits(bytes, at);
        if exponent == 0 {
            return false;
        }
        at = at.saturating_add(exponent);
    }
    at == bytes.len()
}

/// Reads TEXT exactly, an INTEGER as its digits and a REAL as shortest round-trip text.
///
/// A REAL uses Rust's `{:?}`, which names the same decimal value as Python's `repr` though the text can
/// differ (for example `1e21` versus `1e+21`).
///
/// # Errors
/// A BLOB, invalid decimal text and a non-finite REAL.
pub fn decode(value: ValueRef<'_>) -> Result<Decimal, DecodeError> {
    let stored = match value {
        ValueRef::Text(_) => text::decode_str(value)?.to_owned(),
        ValueRef::Integer(int) => int.to_string(),
        ValueRef::Real(float) => format!("{float:?}"),
        other => return Err(mismatch("TEXT, INTEGER or REAL for a DECIMAL", other)),
    };
    Decimal::parse(&stored)
        .ok_or_else(|| DecodeError::new("the stored value is not a finite decimal number"))
}

#[must_use]
pub fn encode(value: &Decimal) -> Value {
    Value::Text(value.0.clone())
}

#[cfg(test)]
mod tests {
    use super::{Decimal, decode, encode};
    use rusqlite::types::{Value, ValueRef};

    fn text(value: &str) -> Result<String, String> {
        decode(ValueRef::Text(value.as_bytes()))
            .map(|decimal| decimal.as_str().to_owned())
            .map_err(|err| err.to_string())
    }

    #[test]
    fn valid_syntax_is_kept_exactly() {
        for value in [
            "0",
            "1",
            "-1",
            "+1",
            "1.50",
            "-0.0",
            "123456789012345678901234567890.123456789",
            ".5",
            "5.",
            "1e10",
            "1E-5",
            "-1.5e+3",
            "0.000",
        ] {
            assert_eq!(text(value), Ok(value.to_owned()), "{value}");
            assert!(Decimal::parse(value).is_some(), "{value}");
        }
    }

    #[test]
    fn invalid_syntax_is_rejected() {
        for value in [
            "", " ", "abc", "1.2.3", "--1", "1e", "e5", ".", "+", "-", "1 ", " 1", "1,5", "NaN",
            "Infinity", "-inf", "0x10", "1_000", "1e+",
        ] {
            assert!(text(value).is_err(), "{value}");
            assert!(Decimal::parse(value).is_none(), "{value}");
        }
    }

    #[test]
    fn integers_and_reals_read_as_decimals() {
        assert_eq!(
            decode(ValueRef::Integer(-42)).map(|d| d.as_str().to_owned()),
            Ok("-42".to_owned())
        );
        for (float, expected) in [
            (0.1, "0.1"),
            (1.0, "1.0"),
            (-2.5, "-2.5"),
            (1e21, "1e21"),
            (1.5e-7, "1.5e-7"),
            (0.1_f64 + 0.2, "0.30000000000000004"),
        ] {
            assert_eq!(
                decode(ValueRef::Real(float)).map(|d| d.as_str().to_owned()),
                Ok(expected.to_owned())
            );
        }
        assert!(decode(ValueRef::Real(f64::INFINITY)).is_err());
        assert!(decode(ValueRef::Real(f64::NAN)).is_err());
    }

    #[test]
    fn blob_and_null_are_errors() {
        assert!(decode(ValueRef::Blob(b"1")).is_err());
        assert!(decode(ValueRef::Null).is_err());
        assert!(decode(ValueRef::Text(&[0xff])).is_err());
    }

    #[test]
    fn decimals_are_written_as_text() {
        let decimal = Decimal::parse("12.50");
        assert_eq!(
            decimal.as_ref().map(encode),
            Some(Value::Text("12.50".to_owned()))
        );
    }
}

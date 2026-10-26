use thiserror::Error;

// the server caps a bit string at this many bits (VARBITMAXLEN)
const MAX_BITS: usize = 83_886_080;

#[derive(Debug, Error, PartialEq, Eq)]
pub enum BitsError {
    #[error("a bit string needs at least 4 bytes, got {0}")]
    Short(usize),
    #[error("the bit length {0} is negative or too large")]
    Length(i64),
    #[error("a bit string of {bits} bits needs {expected} data bytes, got {got}")]
    Data {
        bits: usize,
        expected: usize,
        got: usize,
    },
    #[error("{0:?} is not a bit string: use only `0` and `1`")]
    Syntax(String),
}

/// The `0` and `1` text of a `bit` or `varbit` value: an `i32` bit length, then packed bytes, high bit first.
///
/// # Errors
/// `BitsError` for a malformed value. Padding bits in the last byte are ignored, as the server does.
pub fn decode(raw: &[u8]) -> Result<String, BitsError> {
    let (head, data) = raw
        .split_first_chunk::<4>()
        .ok_or(BitsError::Short(raw.len()))?;
    let declared = i32::from_be_bytes(*head);
    let bits = usize::try_from(declared)
        .ok()
        .filter(|bits| *bits <= MAX_BITS)
        .ok_or_else(|| BitsError::Length(i64::from(declared)))?;
    let expected = bits.div_ceil(8);
    if data.len() != expected {
        return Err(BitsError::Data {
            bits,
            expected,
            got: data.len(),
        });
    }
    Ok((0..bits)
        .map(|i| {
            let byte = data.get(i / 8).copied().unwrap_or(0);
            if byte & (0x80 >> (i % 8)) == 0 {
                '0'
            } else {
                '1'
            }
        })
        .collect())
}

/// # Errors
/// `BitsError::Syntax` for any character other than `0` and `1`, `Length` for a string beyond the server's cap.
pub fn encode(text: &str) -> Result<Vec<u8>, BitsError> {
    if !text.bytes().all(|b| b == b'0' || b == b'1') {
        return Err(BitsError::Syntax(text.to_owned()));
    }
    let bits = text.len();
    if bits > MAX_BITS {
        return Err(BitsError::Length(i64::try_from(bits).unwrap_or(i64::MAX)));
    }
    let declared = i32::try_from(bits).map_err(|_| BitsError::Length(i64::MAX))?;
    let mut out = declared.to_be_bytes().to_vec();
    out.resize(4 + bits.div_ceil(8), 0);
    for (i, b) in text.bytes().enumerate() {
        if b == b'1'
            && let Some(byte) = out.get_mut(4 + i / 8)
        {
            *byte |= 0x80 >> (i % 8);
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use proptest::prelude::*;

    use super::{BitsError, decode, encode};

    #[test]
    fn empty_string_is_just_a_length() {
        assert_eq!(encode(""), Ok(vec![0, 0, 0, 0]));
        assert_eq!(decode(&[0, 0, 0, 0]), Ok(String::new()));
    }

    #[test]
    fn packs_high_bit_first() {
        assert_eq!(encode("1"), Ok(vec![0, 0, 0, 1, 0x80]));
        assert_eq!(encode("0000000"), Ok(vec![0, 0, 0, 7, 0]));
        assert_eq!(encode("10000001"), Ok(vec![0, 0, 0, 8, 0x81]));
        assert_eq!(encode("111111111"), Ok(vec![0, 0, 0, 9, 0xff, 0x80]));
    }

    #[test]
    fn decode_ignores_padding_bits() {
        assert_eq!(decode(&[0, 0, 0, 3, 0b1011_1111]), Ok("101".to_owned()));
    }

    #[test]
    fn decode_rejects_malformed_values() {
        assert!(matches!(decode(&[0, 0]), Err(BitsError::Short(2))));
        assert!(matches!(
            decode(&[0xff, 0xff, 0xff, 0xff]),
            Err(BitsError::Length(-1))
        ));
        assert!(matches!(
            decode(&[0, 0, 0, 9, 0xff]),
            Err(BitsError::Data {
                expected: 2,
                got: 1,
                ..
            })
        ));
        assert!(matches!(
            decode(&[0, 0, 0, 1, 0x80, 0x00]),
            Err(BitsError::Data {
                expected: 1,
                got: 2,
                ..
            })
        ));
    }

    #[test]
    fn encode_rejects_other_characters() {
        assert!(matches!(encode("102"), Err(BitsError::Syntax(_))));
        assert!(matches!(encode(" 1"), Err(BitsError::Syntax(_))));
    }

    proptest! {
        #[test]
        fn round_trips_any_length(text in "[01]{0,200}") {
            prop_assert_eq!(encode(&text).and_then(|raw| decode(&raw)), Ok(text));
        }

        #[test]
        fn decode_never_panics(raw in proptest::collection::vec(any::<u8>(), 0..32)) {
            let _ = decode(&raw);
        }
    }
}

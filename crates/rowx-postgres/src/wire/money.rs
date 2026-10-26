use thiserror::Error;

pub const MONEY_LEN: usize = 8;

#[derive(Debug, Error, PartialEq, Eq)]
pub enum MoneyError {
    #[error("money needs {MONEY_LEN} bytes, got {0}")]
    Length(usize),
}

/// The minor units `cash_send` writes. The scale lives in `lc_monetary`, so the value carries none.
///
/// # Errors
/// `MoneyError::Length` for a value that is not 8 bytes.
pub fn decode(raw: &[u8]) -> Result<i64, MoneyError> {
    <[u8; MONEY_LEN]>::try_from(raw)
        .map(i64::from_be_bytes)
        .map_err(|_| MoneyError::Length(raw.len()))
}

#[must_use]
pub fn encode(minor_units: i64) -> Vec<u8> {
    minor_units.to_be_bytes().to_vec()
}

#[cfg(test)]
mod tests {
    use proptest::prelude::*;

    use super::{MoneyError, decode, encode};

    #[test]
    fn decodes_big_endian_minor_units() {
        assert_eq!(decode(&[0, 0, 0, 0, 0, 0, 0x04, 0xd2]), Ok(1234));
        assert_eq!(decode(&[0xff; 8]), Ok(-1));
    }

    #[test]
    fn rejects_other_lengths() {
        assert_eq!(decode(&[0; 7]), Err(MoneyError::Length(7)));
        assert_eq!(decode(&[]), Err(MoneyError::Length(0)));
    }

    proptest! {
        #[test]
        fn round_trips(value in any::<i64>()) {
            prop_assert_eq!(decode(&encode(value)), Ok(value));
        }

        #[test]
        fn decode_never_panics(raw in proptest::collection::vec(any::<u8>(), 0..16)) {
            let _ = decode(&raw);
        }
    }
}

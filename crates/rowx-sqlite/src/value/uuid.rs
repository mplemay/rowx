use std::fmt;

use rusqlite::types::{Value, ValueRef};

use super::{DecodeError, mismatch, text};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Uuid(pub [u8; 16]);

impl fmt::Display for Uuid {
    // lowercase, hyphenated 8-4-4-4-12
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        for (at, byte) in self.0.iter().enumerate() {
            if matches!(at, 4 | 6 | 8 | 10) {
                f.write_str("-")?;
            }
            write!(f, "{byte:02x}")?;
        }
        Ok(())
    }
}

impl Uuid {
    /// Parses the hyphenated form in either case.
    #[must_use]
    pub fn parse(text: &str) -> Option<Self> {
        let mut nibbles = Vec::with_capacity(32);
        let mut count = 0usize;
        for (at, byte) in text.bytes().enumerate() {
            count = count.saturating_add(1);
            if matches!(at, 8 | 13 | 18 | 23) {
                if byte != b'-' {
                    return None;
                }
            } else {
                nibbles.push(
                    char::from(byte)
                        .to_digit(16)
                        .and_then(|d| u8::try_from(d).ok())?,
                );
            }
        }
        if count != 36 {
            return None;
        }
        let bytes: Vec<u8> = nibbles
            .as_chunks::<2>()
            .0
            .iter()
            .map(|[high, low]| high << 4 | low)
            .collect();
        <[u8; 16]>::try_from(bytes).ok().map(Self)
    }
}

/// Reads TEXT in either case, or a 16-byte BLOB.
///
/// # Errors
/// Anything else.
pub fn decode(value: ValueRef<'_>) -> Result<Uuid, DecodeError> {
    match value {
        ValueRef::Text(_) => Uuid::parse(text::decode_str(value)?)
            .ok_or_else(|| DecodeError::new("the stored TEXT is not a hyphenated UUID")),
        ValueRef::Blob(bytes) => <[u8; 16]>::try_from(bytes).map(Uuid).map_err(|_| {
            DecodeError::new(format!(
                "a UUID BLOB must be 16 bytes but the stored one is {} bytes",
                bytes.len()
            ))
        }),
        other => Err(mismatch("TEXT or a 16-byte BLOB for a UUID", other)),
    }
}

#[must_use]
pub fn encode(value: &Uuid) -> Value {
    Value::Text(value.to_string())
}

#[cfg(test)]
mod tests {
    use super::{Uuid, decode, encode};
    use rusqlite::types::{Value, ValueRef};

    const BYTES: [u8; 16] = [
        0x12, 0x3e, 0x45, 0x67, 0xe8, 0x9b, 0x12, 0xd3, 0xa4, 0x56, 0x42, 0x66, 0x14, 0x17, 0x40,
        0x00,
    ];
    const TEXT: &str = "123e4567-e89b-12d3-a456-426614174000";

    #[test]
    fn writes_lowercase_hyphenated_text() {
        assert_eq!(Uuid(BYTES).to_string(), TEXT);
        assert_eq!(encode(&Uuid(BYTES)), Value::Text(TEXT.to_owned()));
    }

    #[test]
    fn reads_either_case_and_sixteen_byte_blobs() {
        assert_eq!(decode(ValueRef::Text(TEXT.as_bytes())), Ok(Uuid(BYTES)));
        assert_eq!(
            decode(ValueRef::Text(TEXT.to_uppercase().as_bytes())),
            Ok(Uuid(BYTES))
        );
        assert_eq!(decode(ValueRef::Blob(&BYTES)), Ok(Uuid(BYTES)));
    }

    #[test]
    fn anything_else_raises() {
        let unhyphenated = TEXT.replace('-', "");
        let misplaced = "123e4567e-89b-12d3-a456-426614174000";
        let non_hex = "123e4567-e89b-12d3-a456-42661417400g";
        let multibyte = "123e4567-e89b-12d3-a456-42661417400é";
        for value in [
            ValueRef::Text(b""),
            ValueRef::Text(unhyphenated.as_bytes()),
            ValueRef::Text(misplaced.as_bytes()),
            ValueRef::Text(non_hex.as_bytes()),
            ValueRef::Text(multibyte.as_bytes()),
            ValueRef::Text(TEXT.as_bytes().split_at(35).0),
            ValueRef::Text(&[0xff]),
            ValueRef::Blob(BYTES.split_at(15).0),
            ValueRef::Blob(&[0; 17]),
            ValueRef::Integer(1),
            ValueRef::Real(1.0),
            ValueRef::Null,
        ] {
            assert!(decode(value).is_err(), "{value:?}");
        }
    }
}

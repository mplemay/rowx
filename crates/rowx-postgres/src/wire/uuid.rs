use thiserror::Error;

#[derive(Debug, Error, PartialEq, Eq)]
#[error("the text is not a valid uuid")]
pub struct UuidError;

fn nibble(byte: u8) -> Option<u8> {
    char::from(byte)
        .to_digit(16)
        .and_then(|digit| u8::try_from(digit).ok())
}

/// Parses the forms the server's `uuid_in` accepts: 32 hex digits, optionally wrapped in braces, with an
/// optional hyphen after any group of four digits except the last.
///
/// # Errors
/// `UuidError` for anything else, so a malformed value never reaches the server.
pub fn parse(text: &str) -> Result<[u8; 16], UuidError> {
    let mut rest = text.as_bytes();
    let braced = rest.first() == Some(&b'{');
    if braced {
        rest = rest.get(1..).ok_or(UuidError)?;
    }
    let mut out = [0u8; 16];
    for (index, slot) in out.iter_mut().enumerate() {
        let (Some(&high), Some(&low)) = (rest.first(), rest.get(1)) else {
            return Err(UuidError);
        };
        *slot = nibble(high).ok_or(UuidError)? << 4 | nibble(low).ok_or(UuidError)?;
        rest = rest.get(2..).ok_or(UuidError)?;
        if rest.first() == Some(&b'-') && index % 2 == 1 && index < 15 {
            rest = rest.get(1..).ok_or(UuidError)?;
        }
    }
    if braced {
        rest = match rest.split_first() {
            Some((b'}', tail)) => tail,
            _ => return Err(UuidError),
        };
    }
    if rest.is_empty() {
        Ok(out)
    } else {
        Err(UuidError)
    }
}

#[cfg(test)]
mod tests {
    use std::fmt::Write;

    use super::*;

    const EXPECTED: [u8; 16] = [
        0xa0, 0xee, 0xbc, 0x99, 0x9c, 0x0b, 0x4e, 0xf8, 0xbb, 0x6d, 0x6b, 0xb9, 0xbd, 0x38, 0x0a,
        0x11,
    ];

    #[test]
    fn accepts_the_forms_postgres_accepts() {
        for text in [
            "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
            "A0EEBC99-9C0B-4EF8-BB6D-6BB9BD380A11",
            "a0eebc999c0b4ef8bb6d6bb9bd380a11",
            "{a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11}",
            "a0ee-bc99-9c0b-4ef8-bb6d-6bb9-bd38-0a11",
            "{a0eebc999c0b4ef8bb6d6bb9bd380a11}",
        ] {
            assert_eq!(parse(text), Ok(EXPECTED), "{text}");
        }
    }

    #[test]
    fn rejects_everything_else() {
        for text in [
            "",
            "{",
            "{}",
            "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a1",
            "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a111",
            "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11-",
            "{a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
            "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11}",
            "a0eebc99--9c0b-4ef8-bb6d-6bb9bd380a11",
            "a0e-ebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
            "g0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
            "urn:uuid:a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
            " a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
            "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a1\u{ff11}",
            "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11\0",
        ] {
            assert_eq!(parse(text), Err(UuidError), "{text:?}");
        }
    }

    proptest::proptest! {
        #[test]
        fn never_panics(text in ".{0,48}") {
            let _ = parse(&text);
        }

        #[test]
        fn round_trips_any_sixteen_bytes(bytes in proptest::array::uniform16(proptest::prelude::any::<u8>())) {
            let hex = bytes.iter().fold(String::new(), |mut out, b| {
                let _ = write!(out, "{b:02x}");
                out
            });
            proptest::prop_assert_eq!(parse(&hex), Ok(bytes));
        }
    }
}

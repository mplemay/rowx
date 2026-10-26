use std::fmt::Write;

use thiserror::Error;

pub const MAC_LEN: usize = 6;
pub const MAC8_LEN: usize = 8;

#[derive(Debug, Error, PartialEq, Eq)]
pub enum MacError {
    #[error("{ty} needs {expected} bytes, got {got}")]
    Length {
        ty: &'static str,
        expected: usize,
        got: usize,
    },
    #[error("{0:?} is not a MAC address: use hex pairs separated by `:` or `-`")]
    Syntax(String),
    #[error("the address has {got} bytes, but the column takes {expected}")]
    Width { expected: usize, got: usize },
}

const fn ty_name(width: usize) -> &'static str {
    if width == MAC_LEN {
        "macaddr"
    } else {
        "macaddr8"
    }
}

/// The lower case, colon separated text of a `macaddr` or `macaddr8` value.
///
/// # Errors
/// `MacError::Length` when the width is wrong for the type.
pub fn decode(raw: &[u8], width: usize) -> Result<String, MacError> {
    if raw.len() != width {
        return Err(MacError::Length {
            ty: ty_name(width),
            expected: width,
            got: raw.len(),
        });
    }
    let mut out = String::with_capacity(width * 3);
    for (index, byte) in raw.iter().enumerate() {
        if index > 0 {
            out.push(':');
        }
        let _ = write!(out, "{byte:02x}");
    }
    Ok(out)
}

fn hex_pairs(text: &str) -> Option<Vec<u8>> {
    let separator = if text.contains(':') {
        Some(':')
    } else if text.contains('-') {
        Some('-')
    } else {
        None
    };
    let digits: Vec<&str> = if let Some(sep) = separator {
        text.split(sep).collect()
    } else {
        if !text.is_ascii() || text.len() % 2 != 0 {
            return None;
        }
        (0..text.len() / 2)
            .filter_map(|i| text.get(i * 2..i * 2 + 2))
            .collect()
    };
    digits
        .into_iter()
        .map(|pair| {
            (pair.len() == 2 && pair.bytes().all(|b| b.is_ascii_hexdigit()))
                .then(|| u8::from_str_radix(pair, 16).ok())
                .flatten()
        })
        .collect()
}

/// Parses `08:00:2b:01:02:03` (or `-` separated, or twelve bare digits). A `macaddr8` also
/// takes six bytes, which the server widens with `ff:fe` in the middle.
///
/// # Errors
/// `MacError` for any other shape.
pub fn encode(text: &str, width: usize) -> Result<Vec<u8>, MacError> {
    let bytes = hex_pairs(text).ok_or_else(|| MacError::Syntax(text.to_owned()))?;
    if bytes.len() == width {
        return Ok(bytes);
    }
    if width == MAC8_LEN && bytes.len() == MAC_LEN {
        let (head, tail) = bytes.split_at(3);
        return Ok([head, &[0xff, 0xfe], tail].concat());
    }
    Err(MacError::Width {
        expected: width,
        got: bytes.len(),
    })
}

#[cfg(test)]
mod tests {
    use proptest::prelude::*;

    use super::{MAC_LEN, MAC8_LEN, MacError, decode, encode};

    #[test]
    fn decodes_to_lower_case_colons() {
        assert_eq!(
            decode(&[8, 0, 0x2b, 1, 2, 0xAB], MAC_LEN),
            Ok("08:00:2b:01:02:ab".to_owned())
        );
    }

    #[test]
    fn decode_checks_the_width() {
        assert!(matches!(
            decode(&[1, 2], MAC_LEN),
            Err(MacError::Length { .. })
        ));
        assert!(matches!(
            decode(&[0; 6], MAC8_LEN),
            Err(MacError::Length { .. })
        ));
    }

    #[test]
    fn encodes_the_accepted_spellings() {
        let want = vec![8, 0, 0x2b, 1, 2, 3];
        assert_eq!(encode("08:00:2B:01:02:03", MAC_LEN), Ok(want.clone()));
        assert_eq!(encode("08-00-2b-01-02-03", MAC_LEN), Ok(want.clone()));
        assert_eq!(encode("08002b010203", MAC_LEN), Ok(want));
    }

    #[test]
    fn macaddr8_widens_six_bytes_like_the_server() {
        assert_eq!(
            encode("08:00:2b:01:02:03", MAC8_LEN),
            Ok(vec![8, 0, 0x2b, 0xff, 0xfe, 1, 2, 3])
        );
    }

    #[test]
    fn rejects_bad_text_and_widths() {
        assert!(matches!(
            encode("zz:00:2b:01:02:03", MAC_LEN),
            Err(MacError::Syntax(_))
        ));
        assert!(matches!(
            encode("08:00:2b", MAC_LEN),
            Err(MacError::Width { .. })
        ));
        assert!(matches!(
            encode("08:00:2b:01:02:03:04:05", MAC_LEN),
            Err(MacError::Width { .. })
        ));
        assert!(matches!(encode("é", MAC_LEN), Err(MacError::Syntax(_))));
    }

    proptest! {
        #[test]
        fn text_round_trips(raw in any::<[u8; 8]>()) {
            let text = decode(&raw, MAC8_LEN);
            prop_assert_eq!(text.as_deref().map(|t| encode(t, MAC8_LEN)), Ok(Ok(raw.to_vec())));
        }
    }
}

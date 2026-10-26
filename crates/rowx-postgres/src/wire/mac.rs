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

#[cfg(test)]
mod tests {
    use super::{MAC_LEN, MAC8_LEN, MacError, decode};

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
}

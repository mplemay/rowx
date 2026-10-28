use thiserror::Error;

/// The only version `ltree_send` and `lquery_send` write.
pub const VERSION: u8 = 1;

#[derive(Debug, Error, PartialEq, Eq)]
pub enum LtreeError {
    #[error("an {0} value is empty, so it has no version byte")]
    Empty(&'static str),
    #[error("an {name} value has the unknown binary version {version}")]
    Version { name: &'static str, version: u8 },
    #[error("an {0} value is not valid UTF-8")]
    Utf8(&'static str),
}

/// The text behind the version byte of an `ltree` or `lquery` value; `name` only labels errors.
///
/// # Errors
/// `LtreeError` for an empty value, a version other than 1 or text that is not UTF-8.
pub fn decode<'a>(raw: &'a [u8], name: &'static str) -> Result<&'a str, LtreeError> {
    let (version, text) = raw.split_first().ok_or(LtreeError::Empty(name))?;
    if *version != VERSION {
        return Err(LtreeError::Version {
            name,
            version: *version,
        });
    }
    std::str::from_utf8(text).map_err(|_| LtreeError::Utf8(name))
}

#[must_use]
pub fn encode(text: &str) -> Vec<u8> {
    let mut out = Vec::with_capacity(text.len() + 1);
    out.push(VERSION);
    out.extend_from_slice(text.as_bytes());
    out
}

#[cfg(test)]
mod tests {
    use proptest::prelude::*;

    use super::{LtreeError, decode, encode};

    #[test]
    fn a_version_byte_precedes_the_text() {
        assert_eq!(encode("a.b"), [1, b'a', b'.', b'b']);
        assert_eq!(decode(&[1, b'a', b'.', b'b'], "ltree"), Ok("a.b"));
    }

    #[test]
    fn the_empty_label_path_is_just_the_version() {
        assert_eq!(decode(&[1], "ltree"), Ok(""));
    }

    #[test]
    fn an_unknown_version_is_rejected() {
        assert_eq!(
            decode(&[2, b'a'], "lquery"),
            Err(LtreeError::Version {
                name: "lquery",
                version: 2
            })
        );
        assert_eq!(decode(&[], "ltree"), Err(LtreeError::Empty("ltree")));
        assert_eq!(decode(&[1, 0xff], "ltree"), Err(LtreeError::Utf8("ltree")));
    }

    proptest! {
        #[test]
        fn encode_then_decode_is_the_identity(text in any::<String>()) {
            let raw = encode(&text);
            prop_assert_eq!(decode(&raw, "ltree"), Ok(text.as_str()));
        }
    }
}

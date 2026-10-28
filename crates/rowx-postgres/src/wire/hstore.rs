use thiserror::Error;

const COUNT_LEN: usize = 4;
const NULL_LEN: i32 = -1;

#[derive(Debug, Error, PartialEq, Eq)]
pub enum HstoreError {
    #[error("an hstore value is too short")]
    Short,
    #[error("an hstore value has {0} trailing bytes")]
    Trailing(usize),
    #[error("an hstore value has a negative pair count of {0}")]
    Count(i32),
    #[error("an hstore key has a length of {0}")]
    KeyLength(i32),
    #[error("an hstore value has a length of {0}")]
    ValueLength(i32),
    #[error("an hstore string is not valid UTF-8")]
    Utf8,
    #[error("an hstore key or value is longer than 2 GiB")]
    TooLong,
}

pub type Pairs = Vec<(String, Option<String>)>;

struct Reader<'a> {
    rest: &'a [u8],
}

impl Reader<'_> {
    fn int(&mut self) -> Result<i32, HstoreError> {
        let (head, tail) = self
            .rest
            .split_first_chunk::<4>()
            .ok_or(HstoreError::Short)?;
        self.rest = tail;
        Ok(i32::from_be_bytes(*head))
    }

    fn text(&mut self, len: usize) -> Result<String, HstoreError> {
        let (head, tail) = self.rest.split_at_checked(len).ok_or(HstoreError::Short)?;
        self.rest = tail;
        String::from_utf8(head.to_vec()).map_err(|_| HstoreError::Utf8)
    }
}

/// The pairs `hstore_send` writes, in the order the server sent them.
///
/// # Errors
/// An `HstoreError` for a truncated, negative-length, non-UTF-8 or over-long value.
pub fn decode(raw: &[u8]) -> Result<Pairs, HstoreError> {
    let mut reader = Reader { rest: raw };
    let count = reader.int()?;
    let count = usize::try_from(count).map_err(|_| HstoreError::Count(count))?;
    // the count is untrusted, so the vector grows as pairs really arrive
    let mut pairs = Vec::new();
    for _ in 0..count {
        let key_len = reader.int()?;
        let key =
            reader.text(usize::try_from(key_len).map_err(|_| HstoreError::KeyLength(key_len))?)?;
        let value_len = reader.int()?;
        let value = if value_len == NULL_LEN {
            None
        } else {
            Some(reader.text(
                usize::try_from(value_len).map_err(|_| HstoreError::ValueLength(value_len))?,
            )?)
        };
        pairs.push((key, value));
    }
    if reader.rest.is_empty() {
        Ok(pairs)
    } else {
        Err(HstoreError::Trailing(reader.rest.len()))
    }
}

fn push_len(out: &mut Vec<u8>, len: usize) -> Result<(), HstoreError> {
    let len = i32::try_from(len).map_err(|_| HstoreError::TooLong)?;
    out.extend_from_slice(&len.to_be_bytes());
    Ok(())
}

/// # Errors
/// `HstoreError::TooLong` for a count, key or value past `i32::MAX`.
pub fn encode(pairs: &[(String, Option<String>)]) -> Result<Vec<u8>, HstoreError> {
    let mut out = Vec::with_capacity(COUNT_LEN);
    push_len(&mut out, pairs.len())?;
    for (key, value) in pairs {
        push_len(&mut out, key.len())?;
        out.extend_from_slice(key.as_bytes());
        match value {
            Some(value) => {
                push_len(&mut out, value.len())?;
                out.extend_from_slice(value.as_bytes());
            }
            None => out.extend_from_slice(&NULL_LEN.to_be_bytes()),
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use proptest::prelude::*;

    use super::{HstoreError, Pairs, decode, encode};

    fn pair(key: &str, value: Option<&str>) -> (String, Option<String>) {
        (key.to_owned(), value.map(str::to_owned))
    }

    #[test]
    fn decodes_the_wire_layout() {
        let raw = [
            0, 0, 0, 2, // two pairs
            0, 0, 0, 1, b'a', 0, 0, 0, 2, b'x', b'y', // a => xy
            0, 0, 0, 1, b'b', 0xff, 0xff, 0xff, 0xff, // b => NULL
        ];
        assert_eq!(
            decode(&raw),
            Ok(vec![pair("a", Some("xy")), pair("b", None)])
        );
    }

    #[test]
    fn an_empty_value_differs_from_a_null_one() {
        let raw = encode(&[pair("k", Some("")), pair("n", None)]).unwrap_or_default();
        assert_eq!(decode(&raw), Ok(vec![pair("k", Some("")), pair("n", None)]));
    }

    #[test]
    fn an_empty_hstore_is_just_a_zero_count() {
        assert_eq!(encode(&[]), Ok(vec![0, 0, 0, 0]));
        assert_eq!(decode(&[0, 0, 0, 0]), Ok(vec![]));
    }

    #[test]
    fn rejects_malformed_values() {
        assert_eq!(decode(&[0, 0]), Err(HstoreError::Short));
        assert_eq!(
            decode(&[0xff, 0xff, 0xff, 0xff]),
            Err(HstoreError::Count(-1))
        );
        // a count of one with no pair behind it
        assert_eq!(decode(&[0, 0, 0, 1]), Err(HstoreError::Short));
        // a NULL key
        assert_eq!(
            decode(&[0, 0, 0, 1, 0xff, 0xff, 0xff, 0xff]),
            Err(HstoreError::KeyLength(-1))
        );
        // a key longer than the data
        assert_eq!(
            decode(&[0, 0, 0, 1, 0, 0, 0, 9, b'a']),
            Err(HstoreError::Short)
        );
        assert_eq!(
            decode(&[0, 0, 0, 1, 0, 0, 0, 1, 0xff, 0xff, 0xff, 0xff, 0xff]),
            Err(HstoreError::Utf8)
        );
        assert_eq!(
            decode(&[0, 0, 0, 1, 0, 0, 0, 1, b'a', 0xff, 0xff, 0xff, 0xfe]),
            Err(HstoreError::ValueLength(-2))
        );
        assert_eq!(decode(&[0, 0, 0, 0, 7]), Err(HstoreError::Trailing(1)));
    }

    #[test]
    fn a_huge_count_with_no_data_does_not_allocate() {
        assert_eq!(decode(&[0x7f, 0xff, 0xff, 0xff]), Err(HstoreError::Short));
    }

    proptest! {
        #[test]
        fn encode_then_decode_is_the_identity(
            pairs in proptest::collection::vec(
                (any::<String>(), proptest::option::of(any::<String>())),
                0..8,
            )
        ) {
            let pairs: Pairs = pairs;
            let raw = encode(&pairs);
            prop_assert!(raw.is_ok());
            prop_assert_eq!(decode(&raw.unwrap_or_default()), Ok(pairs));
        }

        #[test]
        fn decode_never_panics(raw in proptest::collection::vec(any::<u8>(), 0..64)) {
            let _ = decode(&raw);
        }
    }
}

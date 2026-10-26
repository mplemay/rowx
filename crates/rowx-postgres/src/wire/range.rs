use thiserror::Error;

const EMPTY: u8 = 0x01;
const LOWER_INCLUSIVE: u8 = 0x02;
const UPPER_INCLUSIVE: u8 = 0x04;
const LOWER_INFINITE: u8 = 0x08;
const UPPER_INFINITE: u8 = 0x10;
const KNOWN_FLAGS: u8 = EMPTY | LOWER_INCLUSIVE | UPPER_INCLUSIVE | LOWER_INFINITE | UPPER_INFINITE;

#[derive(Debug, Error, PartialEq, Eq)]
pub enum RangeError {
    #[error("a range needs at least its flags byte")]
    Truncated,
    #[error("range flags {0:#04x} are not valid")]
    Flags(u8),
    #[error("range ends in the middle of its {0} bound")]
    BoundTruncated(&'static str),
    #[error("range has an invalid {0} bound length {1}")]
    BoundLength(&'static str, i32),
    #[error("range has {0} trailing bytes after its upper bound")]
    Trailing(usize),
    #[error("a range bound is too large to encode")]
    TooLarge,
}

/// One end of a range: unbounded, or a value that is in the range (inclusive) or not (exclusive).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Bound<'a> {
    Unbounded,
    Inclusive(&'a [u8]),
    Exclusive(&'a [u8]),
}

impl<'a> Bound<'a> {
    #[must_use]
    pub const fn value(&self) -> Option<&'a [u8]> {
        match self {
            Self::Unbounded => None,
            Self::Inclusive(raw) | Self::Exclusive(raw) => Some(raw),
        }
    }

    #[must_use]
    pub const fn is_inclusive(&self) -> bool {
        matches!(self, Self::Inclusive(_))
    }
}

/// A decoded binary range; the bounds are the subtype's own binary values.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Range<'a> {
    Empty,
    Bounds { lower: Bound<'a>, upper: Bound<'a> },
}

struct Reader<'a> {
    raw: &'a [u8],
    at: usize,
}

impl<'a> Reader<'a> {
    fn take(&mut self, len: usize) -> Option<&'a [u8]> {
        let end = self.at.checked_add(len)?;
        let slice = self.raw.get(self.at..end)?;
        self.at = end;
        Some(slice)
    }

    fn bound(&mut self, which: &'static str) -> Result<&'a [u8], RangeError> {
        let len = self
            .take(4)
            .and_then(|b| <[u8; 4]>::try_from(b).ok())
            .map(i32::from_be_bytes)
            .ok_or(RangeError::BoundTruncated(which))?;
        let size = usize::try_from(len).map_err(|_| RangeError::BoundLength(which, len))?;
        self.take(size).ok_or(RangeError::BoundTruncated(which))
    }
}

/// # Errors
/// `RangeError` when the bytes are not a well-formed binary range.
pub fn decode(raw: &[u8]) -> Result<Range<'_>, RangeError> {
    let (&flags, body) = raw.split_first().ok_or(RangeError::Truncated)?;
    if flags & !KNOWN_FLAGS != 0 {
        return Err(RangeError::Flags(flags));
    }
    if flags & EMPTY != 0 {
        if flags != EMPTY {
            return Err(RangeError::Flags(flags));
        }
        return if body.is_empty() {
            Ok(Range::Empty)
        } else {
            Err(RangeError::Trailing(body.len()))
        };
    }
    let mut reader = Reader { raw: body, at: 0 };
    // the inclusive flag of an infinite side is meaningless, so it is ignored
    let lower = if flags & LOWER_INFINITE != 0 {
        Bound::Unbounded
    } else {
        let value = reader.bound("lower")?;
        if flags & LOWER_INCLUSIVE != 0 {
            Bound::Inclusive(value)
        } else {
            Bound::Exclusive(value)
        }
    };
    let upper = if flags & UPPER_INFINITE != 0 {
        Bound::Unbounded
    } else {
        let value = reader.bound("upper")?;
        if flags & UPPER_INCLUSIVE != 0 {
            Bound::Inclusive(value)
        } else {
            Bound::Exclusive(value)
        }
    };
    if reader.at != body.len() {
        return Err(RangeError::Trailing(body.len() - reader.at));
    }
    Ok(Range::Bounds { lower, upper })
}

fn push_bound(out: &mut Vec<u8>, raw: &[u8]) -> Result<(), RangeError> {
    let len = i32::try_from(raw.len()).map_err(|_| RangeError::TooLarge)?;
    out.extend_from_slice(&len.to_be_bytes());
    out.extend_from_slice(raw);
    Ok(())
}

/// Binary form of a range.
///
/// # Errors
/// `TooLarge` when a bound does not fit the 4-byte length field.
pub fn encode(range: &Range<'_>) -> Result<Vec<u8>, RangeError> {
    let Range::Bounds { lower, upper } = range else {
        return Ok(vec![EMPTY]);
    };
    let mut flags = 0;
    match lower {
        Bound::Unbounded => flags |= LOWER_INFINITE,
        Bound::Inclusive(_) => flags |= LOWER_INCLUSIVE,
        Bound::Exclusive(_) => {}
    }
    match upper {
        Bound::Unbounded => flags |= UPPER_INFINITE,
        Bound::Inclusive(_) => flags |= UPPER_INCLUSIVE,
        Bound::Exclusive(_) => {}
    }
    let mut out = vec![flags];
    for bound in [lower, upper] {
        if let Some(raw) = bound.value() {
            push_bound(&mut out, raw)?;
        }
    }
    Ok(out)
}

fn push_quoted(out: &mut String, raw: &[u8]) {
    let text = String::from_utf8_lossy(raw);
    let plain = !text.is_empty()
        && !text
            .chars()
            .any(|ch| ch.is_whitespace() || matches!(ch, '"' | '\\' | ',' | '(' | ')' | '[' | ']'))
        && !text.eq_ignore_ascii_case("empty");
    if plain {
        out.push_str(&text);
        return;
    }
    out.push('"');
    for ch in text.chars() {
        if matches!(ch, '"' | '\\') {
            out.push('\\');
        }
        out.push(ch);
    }
    out.push('"');
}

/// Text literal of a range whose bounds are the subtype's text values, such as `[1.5,2.5)`.
#[must_use]
pub fn encode_literal(range: &Range<'_>) -> String {
    let Range::Bounds { lower, upper } = range else {
        return "empty".to_owned();
    };
    let mut out = String::new();
    out.push(if lower.is_inclusive() { '[' } else { '(' });
    if let Some(raw) = lower.value() {
        push_quoted(&mut out, raw);
    }
    out.push(',');
    if let Some(raw) = upper.value() {
        push_quoted(&mut out, raw);
    }
    out.push(if upper.is_inclusive() { ']' } else { ')' });
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bound(raw: &[u8]) -> Vec<u8> {
        let mut out = i32::try_from(raw.len()).unwrap_or(0).to_be_bytes().to_vec();
        out.extend_from_slice(raw);
        out
    }

    #[test]
    fn the_empty_range_is_one_flag_byte() {
        assert_eq!(decode(&[EMPTY]), Ok(Range::Empty));
        assert_eq!(encode(&Range::Empty), Ok(vec![EMPTY]));
    }

    #[test]
    fn a_closed_open_range_carries_both_bounds() {
        let mut raw = vec![LOWER_INCLUSIVE];
        raw.extend(bound(&[0, 0, 0, 1]));
        raw.extend(bound(&[0, 0, 0, 6]));
        let range = Range::Bounds {
            lower: Bound::Inclusive(&[0, 0, 0, 1]),
            upper: Bound::Exclusive(&[0, 0, 0, 6]),
        };
        assert_eq!(decode(&raw), Ok(range));
        assert_eq!(encode(&range), Ok(raw));
    }

    #[test]
    fn infinite_sides_carry_no_bytes() {
        let range = Range::Bounds {
            lower: Bound::Unbounded,
            upper: Bound::Unbounded,
        };
        let raw = vec![LOWER_INFINITE | UPPER_INFINITE];
        assert_eq!(decode(&raw), Ok(range));
        assert_eq!(encode(&range), Ok(raw));
    }

    #[test]
    fn the_inclusive_flag_of_an_infinite_side_is_ignored() {
        let raw = [LOWER_INFINITE | LOWER_INCLUSIVE | UPPER_INFINITE | UPPER_INCLUSIVE];
        assert_eq!(
            decode(&raw),
            Ok(Range::Bounds {
                lower: Bound::Unbounded,
                upper: Bound::Unbounded
            })
        );
    }

    #[test]
    fn a_zero_length_bound_is_a_value() {
        let mut raw = vec![0];
        raw.extend(bound(&[]));
        raw.extend(bound(&[7]));
        assert_eq!(
            decode(&raw),
            Ok(Range::Bounds {
                lower: Bound::Exclusive(&[]),
                upper: Bound::Exclusive(&[7])
            })
        );
    }

    #[test]
    fn rejects_malformed_input() {
        assert_eq!(decode(&[]), Err(RangeError::Truncated));
        assert_eq!(decode(&[0x20]), Err(RangeError::Flags(0x20)));
        assert_eq!(decode(&[0x80]), Err(RangeError::Flags(0x80)));
        assert_eq!(
            decode(&[EMPTY | LOWER_INCLUSIVE]),
            Err(RangeError::Flags(EMPTY | LOWER_INCLUSIVE))
        );
        assert_eq!(decode(&[EMPTY, 0]), Err(RangeError::Trailing(1)));
        assert_eq!(decode(&[0]), Err(RangeError::BoundTruncated("lower")));
        assert_eq!(
            decode(&[UPPER_INFINITE, 0, 0, 0, 4, 1]),
            Err(RangeError::BoundTruncated("lower"))
        );
        let mut negative = vec![0];
        negative.extend((-1_i32).to_be_bytes());
        assert_eq!(decode(&negative), Err(RangeError::BoundLength("lower", -1)));
        let mut missing_upper = vec![0];
        missing_upper.extend(bound(&[1]));
        assert_eq!(
            decode(&missing_upper),
            Err(RangeError::BoundTruncated("upper"))
        );
        let mut trailing = vec![LOWER_INFINITE | UPPER_INFINITE];
        trailing.push(9);
        assert_eq!(decode(&trailing), Err(RangeError::Trailing(1)));
    }

    #[test]
    fn literals_quote_only_what_needs_it() {
        let range = Range::Bounds {
            lower: Bound::Inclusive(b"1.5"),
            upper: Bound::Exclusive(b"2.5"),
        };
        assert_eq!(encode_literal(&range), "[1.5,2.5)");
        let odd = Range::Bounds {
            lower: Bound::Exclusive(b"a b"),
            upper: Bound::Inclusive(b"q\"\\"),
        };
        assert_eq!(encode_literal(&odd), "(\"a b\",\"q\\\"\\\\\"]");
        let open = Range::Bounds {
            lower: Bound::Unbounded,
            upper: Bound::Unbounded,
        };
        assert_eq!(encode_literal(&open), "(,)");
        assert_eq!(encode_literal(&Range::Empty), "empty");
        let word = Range::Bounds {
            lower: Bound::Inclusive(b"empty"),
            upper: Bound::Unbounded,
        };
        assert_eq!(encode_literal(&word), "[\"empty\",)");
    }
}

#[cfg(test)]
mod proptests {
    use super::*;
    use proptest::prelude::*;

    #[derive(Debug, Clone)]
    enum Side {
        Unbounded,
        Inclusive(Vec<u8>),
        Exclusive(Vec<u8>),
    }

    fn side() -> impl Strategy<Value = Side> {
        let value = prop::collection::vec(any::<u8>(), 0..8);
        prop_oneof![
            1 => Just(Side::Unbounded),
            2 => value.clone().prop_map(Side::Inclusive),
            2 => value.prop_map(Side::Exclusive),
        ]
    }

    fn as_bound(side: &Side) -> Bound<'_> {
        match side {
            Side::Unbounded => Bound::Unbounded,
            Side::Inclusive(raw) => Bound::Inclusive(raw),
            Side::Exclusive(raw) => Bound::Exclusive(raw),
        }
    }

    proptest! {
        #[test]
        fn encode_then_decode_is_the_identity(lower in side(), upper in side()) {
            let range = Range::Bounds { lower: as_bound(&lower), upper: as_bound(&upper) };
            let raw = encode(&range).unwrap_or_default();
            prop_assert_eq!(decode(&raw), Ok(range));
        }

        #[test]
        fn every_strict_prefix_of_a_bounded_encoding_is_rejected(
            lower in side(),
            upper in side(),
            cut in 0usize..64,
        ) {
            let range = Range::Bounds { lower: as_bound(&lower), upper: as_bound(&upper) };
            let raw = encode(&range).unwrap_or_default();
            let cut = cut % raw.len();
            prop_assert!(decode(&raw[..cut]).is_err());
        }

        #[test]
        fn decoding_random_bytes_never_panics(raw in prop::collection::vec(any::<u8>(), 0..64)) {
            let _ = decode(&raw);
        }

        #[test]
        fn a_literal_is_bracketed(lower in side(), upper in side()) {
            let range = Range::Bounds { lower: as_bound(&lower), upper: as_bound(&upper) };
            let text = encode_literal(&range);
            prop_assert!(text.starts_with(['[', '(']));
            prop_assert!(text.ends_with([']', ')']));
        }
    }
}

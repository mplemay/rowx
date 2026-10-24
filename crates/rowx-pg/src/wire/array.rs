use thiserror::Error;

const HEADER_LEN: usize = 12;
const MAX_DIMS: i32 = 6;

#[derive(Debug, Error, PartialEq, Eq)]
pub enum ArrayError {
    #[error("array needs at least {HEADER_LEN} header bytes, got {0}")]
    Truncated(usize),
    #[error("array declares {0} dimensions, outside 0..={MAX_DIMS}")]
    Dimensions(i32),
    #[error("array declares a negative length {0}")]
    NegativeLength(i32),
    #[error("array declares more elements than fit in memory")]
    TooLarge,
    #[error("array ends in the middle of element {0}")]
    ElementTruncated(usize),
    #[error("array element {0} has an invalid length {1}")]
    ElementLength(usize, i32),
    #[error("array has {0} trailing bytes after its last element")]
    Trailing(usize),
}

/// A decoded binary array: dimension lengths plus the flat, row-major elements.
///
/// Lower bounds are read and discarded, so `'[5:6]={1,2}'::int[]` decodes like `{1,2}`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Array<'a> {
    pub element_oid: u32,
    pub dims: Vec<usize>,
    pub elements: Vec<Option<&'a [u8]>>,
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

    fn i32(&mut self) -> Option<i32> {
        self.take(4)
            .and_then(|b| <[u8; 4]>::try_from(b).ok())
            .map(i32::from_be_bytes)
    }
}

/// # Errors
/// `ArrayError` when the bytes are not a well-formed binary array.
pub fn decode(raw: &[u8]) -> Result<Array<'_>, ArrayError> {
    let mut reader = Reader { raw, at: 0 };
    let (Some(ndim), Some(_has_null), Some(oid)) = (reader.i32(), reader.i32(), reader.i32())
    else {
        return Err(ArrayError::Truncated(raw.len()));
    };
    if !(0..=MAX_DIMS).contains(&ndim) {
        return Err(ArrayError::Dimensions(ndim));
    }
    let element_oid = u32::from_be_bytes(oid.to_be_bytes());

    let mut dims = Vec::new();
    let mut total: usize = usize::from(ndim != 0);
    for _ in 0..ndim {
        let (Some(len), Some(_lower_bound)) = (reader.i32(), reader.i32()) else {
            return Err(ArrayError::Truncated(raw.len()));
        };
        let len = usize::try_from(len).map_err(|_| ArrayError::NegativeLength(len))?;
        total = total.checked_mul(len).ok_or(ArrayError::TooLarge)?;
        dims.push(len);
    }

    // every element takes at least 4 bytes, which bounds the allocation by the input size
    let mut elements = Vec::with_capacity(total.min(raw.len() / 4));
    for index in 0..total {
        let len = reader.i32().ok_or(ArrayError::ElementTruncated(index))?;
        if len == -1 {
            elements.push(None);
            continue;
        }
        let size = usize::try_from(len).map_err(|_| ArrayError::ElementLength(index, len))?;
        let bytes = reader
            .take(size)
            .ok_or(ArrayError::ElementTruncated(index))?;
        elements.push(Some(bytes));
    }
    if reader.at != raw.len() {
        return Err(ArrayError::Trailing(raw.len() - reader.at));
    }
    Ok(Array {
        element_oid,
        dims,
        elements,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn header(ndim: i32, has_null: i32, oid: i32) -> Vec<u8> {
        [ndim, has_null, oid]
            .iter()
            .flat_map(|v| v.to_be_bytes())
            .collect()
    }

    fn dim(len: i32, lower: i32) -> Vec<u8> {
        [len, lower].iter().flat_map(|v| v.to_be_bytes()).collect()
    }

    fn elem(bytes: Option<&[u8]>) -> Vec<u8> {
        bytes.map_or_else(
            || (-1_i32).to_be_bytes().to_vec(),
            |b| {
                let mut out = i32::try_from(b.len()).unwrap_or(0).to_be_bytes().to_vec();
                out.extend_from_slice(b);
                out
            },
        )
    }

    #[test]
    fn empty_array_has_no_dims() {
        let raw = header(0, 0, 25);
        let array = decode(&raw);
        assert_eq!(
            array,
            Ok(Array {
                element_oid: 25,
                dims: vec![],
                elements: vec![]
            })
        );
    }

    #[test]
    fn one_dimension_with_null() {
        let mut raw = header(1, 1, 23);
        raw.extend(dim(3, 1));
        raw.extend(elem(Some(&[0, 0, 0, 1])));
        raw.extend(elem(None));
        raw.extend(elem(Some(&[0, 0, 0, 3])));
        let array = decode(&raw);
        assert_eq!(
            array,
            Ok(Array {
                element_oid: 23,
                dims: vec![3],
                elements: vec![Some(&[0, 0, 0, 1]), None, Some(&[0, 0, 0, 3])]
            })
        );
    }

    #[test]
    fn two_dimensions_are_row_major() {
        let mut raw = header(2, 0, 23);
        raw.extend(dim(2, 1));
        raw.extend(dim(2, 1));
        for v in 1_u8..=4 {
            raw.extend(elem(Some(&[0, 0, 0, v])));
        }
        let array = decode(&raw);
        assert_eq!(array.as_ref().map(|a| a.dims.clone()), Ok(vec![2, 2]));
        assert_eq!(array.map(|a| a.elements.len()), Ok(4));
    }

    #[test]
    fn lower_bounds_are_ignored() {
        let mut shifted = header(1, 0, 23);
        shifted.extend(dim(2, 5));
        shifted.extend(elem(Some(&[0, 0, 0, 1])));
        shifted.extend(elem(Some(&[0, 0, 0, 2])));
        let mut plain = shifted.clone();
        plain.splice(12..16, 2_i32.to_be_bytes());
        plain.splice(16..20, 1_i32.to_be_bytes());
        assert_eq!(decode(&shifted), decode(&plain));
        assert_eq!(decode(&shifted).map(|a| a.dims), Ok(vec![2]));
    }

    #[test]
    fn zero_length_dimension_has_no_elements() {
        let mut raw = header(1, 0, 25);
        raw.extend(dim(0, 1));
        assert_eq!(
            decode(&raw).map(|a| (a.dims, a.elements.len())),
            Ok((vec![0], 0))
        );
    }

    #[test]
    fn rejects_malformed_input() {
        assert_eq!(decode(&[0; 4]), Err(ArrayError::Truncated(4)));
        assert_eq!(decode(&header(7, 0, 23)), Err(ArrayError::Dimensions(7)));
        assert_eq!(decode(&header(-1, 0, 23)), Err(ArrayError::Dimensions(-1)));

        let mut negative = header(1, 0, 23);
        negative.extend(dim(-2, 1));
        assert_eq!(decode(&negative), Err(ArrayError::NegativeLength(-2)));

        let mut missing = header(1, 0, 23);
        missing.extend(dim(2, 1));
        missing.extend(elem(Some(&[1])));
        assert_eq!(decode(&missing), Err(ArrayError::ElementTruncated(1)));

        let mut short = header(1, 0, 23);
        short.extend(dim(1, 1));
        short.extend(8_i32.to_be_bytes());
        short.extend([1, 2]);
        assert_eq!(decode(&short), Err(ArrayError::ElementTruncated(0)));

        let mut bad_len = header(1, 0, 23);
        bad_len.extend(dim(1, 1));
        bad_len.extend((-5_i32).to_be_bytes());
        assert_eq!(decode(&bad_len), Err(ArrayError::ElementLength(0, -5)));

        let mut trailing = header(0, 0, 23);
        trailing.push(9);
        assert_eq!(decode(&trailing), Err(ArrayError::Trailing(1)));
    }

    #[test]
    fn huge_dimensions_do_not_allocate() {
        let mut raw = header(2, 0, 23);
        raw.extend(dim(i32::MAX, 1));
        raw.extend(dim(i32::MAX, 1));
        assert!(decode(&raw).is_err());
    }
}

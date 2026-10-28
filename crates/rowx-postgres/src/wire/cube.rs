use thiserror::Error;

const HEADER_LEN: usize = 4;
const F64_LEN: usize = 8;
const POINT_BIT: u32 = 0x8000_0000;
const DIM_MASK: u32 = 0x7fff_ffff;
/// `CUBE_MAX_DIM` in contrib/cube.
pub const MAX_DIM: usize = 100;

#[derive(Debug, Error, PartialEq, Eq)]
pub enum CubeError {
    #[error("a cube value is too short")]
    Short,
    #[error("a cube of {dim} dimensions needs {expected} bytes, got {got}")]
    Length {
        dim: usize,
        expected: usize,
        got: usize,
    },
    #[error("a cube has {0} dimensions, and the limit is 100")]
    Dimensions(usize),
    #[error("the cube corners have {lower} and {upper} coordinates, and must have the same number")]
    Corners { lower: usize, upper: usize },
}

#[derive(Debug, Clone, PartialEq)]
pub struct Cube {
    pub lower_left: Vec<f64>,
    pub upper_right: Vec<f64>,
}

fn floats(raw: &[u8], count: usize) -> Vec<f64> {
    raw.chunks_exact(F64_LEN)
        .take(count)
        .filter_map(|chunk| <[u8; F64_LEN]>::try_from(chunk).ok())
        .map(f64::from_be_bytes)
        .collect()
}

/// A cube as `cube_send` writes it: a header (the dimension, and a flag for a point), then the
/// coordinates of the lower left corner and, for a box, of the upper right one.
///
/// # Errors
/// `CubeError` for a truncated value, a length that does not fit the header, or too many dimensions.
pub fn decode(raw: &[u8]) -> Result<Cube, CubeError> {
    let (head, data) = raw
        .split_first_chunk::<HEADER_LEN>()
        .ok_or(CubeError::Short)?;
    let header = u32::from_be_bytes(*head);
    // the dimension is read before it is trusted, so the length check below bounds any allocation
    let dim = usize::try_from(header & DIM_MASK).map_err(|_| CubeError::Dimensions(usize::MAX))?;
    if dim > MAX_DIM {
        return Err(CubeError::Dimensions(dim));
    }
    let is_point = header & POINT_BIT != 0;
    let count = if is_point { dim } else { 2 * dim };
    let expected = HEADER_LEN + count * F64_LEN;
    if raw.len() != expected {
        return Err(CubeError::Length {
            dim,
            expected,
            got: raw.len(),
        });
    }
    let lower_left = floats(data, dim);
    let upper_right = if is_point {
        lower_left.clone()
    } else {
        floats(&data[dim * F64_LEN..], dim)
    };
    Ok(Cube {
        lower_left,
        upper_right,
    })
}

/// A cube whose corners are bit-for-bit equal is written as a point.
///
/// # Errors
/// `CubeError::Corners` for corners of different lengths, `Dimensions` past 100.
pub fn encode(cube: &Cube) -> Result<Vec<u8>, CubeError> {
    let dim = cube.lower_left.len();
    if dim != cube.upper_right.len() {
        return Err(CubeError::Corners {
            lower: dim,
            upper: cube.upper_right.len(),
        });
    }
    if dim > MAX_DIM {
        return Err(CubeError::Dimensions(dim));
    }
    let is_point = cube
        .lower_left
        .iter()
        .zip(&cube.upper_right)
        .all(|(a, b)| a.to_bits() == b.to_bits());
    let dim_bits = u32::try_from(dim).map_err(|_| CubeError::Dimensions(dim))?;
    let header = if is_point {
        dim_bits | POINT_BIT
    } else {
        dim_bits
    };
    let mut out = Vec::with_capacity(HEADER_LEN + 2 * dim * F64_LEN);
    out.extend_from_slice(&header.to_be_bytes());
    let corners: &[&[f64]] = if is_point {
        &[&cube.lower_left]
    } else {
        &[&cube.lower_left, &cube.upper_right]
    };
    for corner in corners {
        for value in *corner {
            out.extend_from_slice(&value.to_be_bytes());
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use proptest::prelude::*;

    use super::{Cube, CubeError, MAX_DIM, decode, encode};

    fn cube(lower: &[f64], upper: &[f64]) -> Cube {
        Cube {
            lower_left: lower.to_vec(),
            upper_right: upper.to_vec(),
        }
    }

    #[test]
    fn a_point_is_written_once_with_the_point_flag() {
        let raw = encode(&cube(&[1.0, 2.0], &[1.0, 2.0])).unwrap_or_default();
        assert_eq!(&raw[..4], [0x80, 0, 0, 2]);
        assert_eq!(raw.len(), 4 + 2 * 8);
        assert_eq!(decode(&raw), Ok(cube(&[1.0, 2.0], &[1.0, 2.0])));
    }

    #[test]
    fn a_box_is_written_as_both_corners() {
        let raw = encode(&cube(&[0.0, 0.0], &[1.0, 2.0])).unwrap_or_default();
        assert_eq!(&raw[..4], [0, 0, 0, 2]);
        assert_eq!(raw.len(), 4 + 4 * 8);
        assert_eq!(decode(&raw), Ok(cube(&[0.0, 0.0], &[1.0, 2.0])));
    }

    #[test]
    fn the_empty_cube_has_no_coordinates() {
        assert_eq!(encode(&cube(&[], &[])), Ok(vec![0x80, 0, 0, 0]));
        assert_eq!(decode(&[0, 0, 0, 0]), Ok(cube(&[], &[])));
    }

    #[test]
    fn rejects_malformed_values() {
        assert_eq!(decode(&[0, 0]), Err(CubeError::Short));
        assert_eq!(
            decode(&[0, 0, 0, 1, 0]),
            Err(CubeError::Length {
                dim: 1,
                expected: 20,
                got: 5
            })
        );
        assert_eq!(
            decode(&[0x7f, 0xff, 0xff, 0xff]),
            Err(CubeError::Dimensions(0x7fff_ffff))
        );
        assert_eq!(
            encode(&cube(&[1.0], &[1.0, 2.0])),
            Err(CubeError::Corners { lower: 1, upper: 2 })
        );
        let wide = vec![0.0; MAX_DIM + 1];
        assert_eq!(
            encode(&cube(&wide, &wide)),
            Err(CubeError::Dimensions(MAX_DIM + 1))
        );
    }

    #[test]
    fn negative_zero_is_not_collapsed_into_a_point() {
        let raw = encode(&cube(&[0.0], &[-0.0])).unwrap_or_default();
        assert_eq!(raw[0], 0);
    }

    proptest! {
        #[test]
        fn encode_then_decode_preserves_both_corners(
            pairs in proptest::collection::vec(
                (-1e9f64..1e9, -1e9f64..1e9),
                0..=MAX_DIM,
            )
        ) {
            let (lower, upper): (Vec<f64>, Vec<f64>) = pairs.into_iter().unzip();
            let original = Cube { lower_left: lower, upper_right: upper };
            let raw = encode(&original);
            prop_assert!(raw.is_ok());
            prop_assert_eq!(decode(&raw.unwrap_or_default()), Ok(original));
        }

        #[test]
        fn decode_never_panics(raw in proptest::collection::vec(any::<u8>(), 0..64)) {
            let _ = decode(&raw);
        }
    }
}

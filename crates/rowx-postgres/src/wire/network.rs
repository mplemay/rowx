use thiserror::Error;

// postgres writes AF_INET as 2 and AF_INET6 as AF_INET + 1, whatever the platform's own values
const FAMILY_V4: u8 = 2;
const FAMILY_V6: u8 = 3;
const V4_LEN: usize = 4;
const V6_LEN: usize = 16;
const HEADER_LEN: usize = 4;

#[derive(Debug, Error, PartialEq, Eq)]
pub enum NetworkError {
    #[error("inet/cidr needs at least {HEADER_LEN} bytes, got {0}")]
    Truncated(usize),
    #[error("inet/cidr has an unknown address family {0}")]
    Family(u8),
    #[error("inet/cidr declares {0} address bytes, which does not fit its family")]
    AddressLength(u8),
    #[error("inet/cidr has {0} bytes, but its header declares {1}")]
    Length(usize, usize),
    #[error("inet/cidr has a prefix of {0} bits, which is out of range for its family")]
    Bits(u8),
    #[error("inet/cidr has an invalid is_cidr flag {0}")]
    Flag(u8),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Addr {
    V4([u8; V4_LEN]),
    V6([u8; V6_LEN]),
}

impl Addr {
    #[must_use]
    pub const fn max_bits(&self) -> u8 {
        match self {
            Self::V4(_) => 32,
            Self::V6(_) => 128,
        }
    }

    #[must_use]
    pub const fn bytes(&self) -> &[u8] {
        match self {
            Self::V4(bytes) => bytes,
            Self::V6(bytes) => bytes,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Network {
    pub addr: Addr,
    pub bits: u8,
    pub is_cidr: bool,
}

/// # Errors
/// `NetworkError` when the bytes are not an `inet_send` or `cidr_send` value.
pub fn decode(raw: &[u8]) -> Result<Network, NetworkError> {
    let [family, bits, flag, len, rest @ ..] = raw else {
        return Err(NetworkError::Truncated(raw.len()));
    };
    let (addr, expected) = match *family {
        FAMILY_V4 => (<[u8; V4_LEN]>::try_from(rest).map(Addr::V4).ok(), V4_LEN),
        FAMILY_V6 => (<[u8; V6_LEN]>::try_from(rest).map(Addr::V6).ok(), V6_LEN),
        other => return Err(NetworkError::Family(other)),
    };
    if usize::from(*len) != expected {
        return Err(NetworkError::AddressLength(*len));
    }
    let addr = addr.ok_or(NetworkError::Length(rest.len(), expected))?;
    if *bits > addr.max_bits() {
        return Err(NetworkError::Bits(*bits));
    }
    let is_cidr = match *flag {
        0 => false,
        1 => true,
        other => return Err(NetworkError::Flag(other)),
    };
    Ok(Network {
        addr,
        bits: *bits,
        is_cidr,
    })
}

/// # Errors
/// `NetworkError::Bits` when the prefix is wider than the address.
pub fn encode(network: &Network) -> Result<Vec<u8>, NetworkError> {
    if network.bits > network.addr.max_bits() {
        return Err(NetworkError::Bits(network.bits));
    }
    let family = match network.addr {
        Addr::V4(_) => FAMILY_V4,
        Addr::V6(_) => FAMILY_V6,
    };
    let bytes = network.addr.bytes();
    let mut out = Vec::with_capacity(HEADER_LEN + bytes.len());
    out.push(family);
    out.push(network.bits);
    out.push(u8::from(network.is_cidr));
    // the address is at most 16 bytes
    out.push(u8::try_from(bytes.len()).unwrap_or(u8::MAX));
    out.extend_from_slice(bytes);
    Ok(out)
}

#[cfg(test)]
mod tests {
    use proptest::prelude::*;

    use super::{Addr, Network, NetworkError, decode, encode};

    #[test]
    fn decodes_an_inet_v4_host() {
        let raw = [2, 32, 0, 4, 127, 0, 0, 1];
        assert_eq!(
            decode(&raw),
            Ok(Network {
                addr: Addr::V4([127, 0, 0, 1]),
                bits: 32,
                is_cidr: false
            })
        );
    }

    #[test]
    fn rejects_malformed_values() {
        assert_eq!(decode(&[2, 32]), Err(NetworkError::Truncated(2)));
        assert_eq!(decode(&[9, 0, 0, 0]), Err(NetworkError::Family(9)));
        assert_eq!(
            decode(&[2, 0, 0, 5, 0]),
            Err(NetworkError::AddressLength(5))
        );
        assert_eq!(decode(&[2, 0, 0, 4, 1]), Err(NetworkError::Length(1, 4)));
        assert_eq!(
            decode(&[2, 33, 0, 4, 1, 2, 3, 4]),
            Err(NetworkError::Bits(33))
        );
        assert_eq!(
            decode(&[2, 8, 2, 4, 1, 2, 3, 4]),
            Err(NetworkError::Flag(2))
        );
    }

    #[test]
    fn encode_rejects_a_wide_prefix() {
        let network = Network {
            addr: Addr::V4([1, 2, 3, 4]),
            bits: 33,
            is_cidr: false,
        };
        assert_eq!(encode(&network), Err(NetworkError::Bits(33)));
    }

    fn network() -> impl Strategy<Value = Network> {
        prop_oneof![
            (any::<[u8; 4]>(), 0u8..=32, any::<bool>()).prop_map(|(a, bits, is_cidr)| Network {
                addr: Addr::V4(a),
                bits,
                is_cidr
            }),
            (any::<[u8; 16]>(), 0u8..=128, any::<bool>()).prop_map(|(a, bits, is_cidr)| Network {
                addr: Addr::V6(a),
                bits,
                is_cidr
            }),
        ]
    }

    proptest! {
        #[test]
        fn encode_then_decode_round_trips(n in network()) {
            let raw = encode(&n);
            prop_assert_eq!(raw.as_deref().map(decode), Ok(Ok(n)));
        }

        #[test]
        fn decode_never_panics(raw in prop::collection::vec(any::<u8>(), 0..24)) {
            let _ = decode(&raw);
        }
    }
}

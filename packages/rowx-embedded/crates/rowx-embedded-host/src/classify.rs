// startup-phase packet classification and frame splitting: pure functions over bytes

use std::collections::BTreeMap;

pub const SSL_REQUEST_CODE: i32 = 80_877_103;
pub const GSSENC_REQUEST_CODE: i32 = 80_877_104;
pub const CANCEL_REQUEST_CODE: i32 = 80_877_102;
pub const PROTOCOL_3: i32 = 196_608;
// PostgreSQL itself refuses a startup packet larger than this
pub const MAX_STARTUP_PACKET: usize = 10_000;
pub const MAX_MESSAGE: usize = 128 * 1024 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Startup {
    // SSLRequest: answered `N`
    Ssl,
    // GSSENCRequest: answered `N`
    Gss,
    // CancelRequest: the socket is closed at once
    Cancel,
    // a protocol 3.0 StartupMessage
    V3,
    // a protocol 3.x StartupMessage with a newer minor version: answered with NegotiateProtocolVersion
    V3Newer(u16),
}

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum FrameError {
    #[error("startup packet is too short ({0} bytes)")]
    TooShort(usize),
    #[error("invalid packet length {0}")]
    BadLength(i32),
    #[error("packet of {0} bytes exceeds the limit")]
    TooLong(usize),
    #[error("unsupported startup code {0}")]
    UnsupportedCode(i32),
    #[error("malformed startup packet: {0}")]
    Malformed(&'static str),
}

fn be_i32(bytes: &[u8], at: usize) -> Option<i32> {
    let slice = bytes.get(at..at + 4)?;
    Some(i32::from_be_bytes(<[u8; 4]>::try_from(slice).ok()?))
}

/// Classifies one complete, length-prefixed startup-phase packet (the length is included).
///
/// # Errors
/// When the packet is too short, its length disagrees with its size, or its request code is unknown.
pub fn classify_startup(packet: &[u8]) -> Result<Startup, FrameError> {
    if packet.len() < 8 {
        return Err(FrameError::TooShort(packet.len()));
    }
    let declared = be_i32(packet, 0).ok_or(FrameError::TooShort(packet.len()))?;
    if usize::try_from(declared).ok() != Some(packet.len()) {
        return Err(FrameError::BadLength(declared));
    }
    match be_i32(packet, 4).ok_or(FrameError::TooShort(packet.len()))? {
        SSL_REQUEST_CODE => Ok(Startup::Ssl),
        GSSENC_REQUEST_CODE => Ok(Startup::Gss),
        CANCEL_REQUEST_CODE => Ok(Startup::Cancel),
        PROTOCOL_3 => Ok(Startup::V3),
        other if other >> 16 == PROTOCOL_3 >> 16 => Ok(Startup::V3Newer(
            u16::try_from(other & 0xffff).unwrap_or(u16::MAX),
        )),
        other => Err(FrameError::UnsupportedCode(other)),
    }
}

/// Decodes the key/value pairs of a protocol 3.0 startup packet, rejecting duplicate keys and trailing bytes.
///
/// # Errors
/// When the packet is not one complete v3 startup message.
pub fn startup_parameters(packet: &[u8]) -> Result<BTreeMap<&str, &str>, FrameError> {
    if !matches!(classify_startup(packet)?, Startup::V3 | Startup::V3Newer(_)) {
        return Err(FrameError::Malformed("not a protocol 3 startup packet"));
    }
    let body = packet.get(8..).ok_or(FrameError::TooShort(packet.len()))?;
    let (terminator, pairs) = body
        .split_last()
        .ok_or(FrameError::Malformed("missing terminator"))?;
    if *terminator != 0 {
        return Err(FrameError::Malformed("missing terminator"));
    }
    let mut parameters = BTreeMap::new();
    let mut fields = pairs.split(|byte| *byte == 0);
    while let Some(key) = fields.next() {
        if key.is_empty() {
            // the empty key is the terminator: nothing may follow it
            return if fields.next().is_none() {
                Ok(parameters)
            } else {
                Err(FrameError::Malformed("bytes after the terminator"))
            };
        }
        let value = fields
            .next()
            .ok_or(FrameError::Malformed("key without a value"))?;
        let key =
            std::str::from_utf8(key).map_err(|_| FrameError::Malformed("key is not UTF-8"))?;
        let value =
            std::str::from_utf8(value).map_err(|_| FrameError::Malformed("value is not UTF-8"))?;
        if parameters.insert(key, value).is_some() {
            return Err(FrameError::Malformed("duplicate key"));
        }
    }
    Err(FrameError::Malformed("missing terminator"))
}

/// Splits a byte stream into startup packets (`len`, payload) and then typed messages (`tag`, `len`, payload).
#[derive(Debug, Default)]
pub struct FrameReader {
    buffer: Vec<u8>,
}

impl FrameReader {
    pub fn push(&mut self, input: &[u8]) {
        self.buffer.extend_from_slice(input);
    }

    #[must_use]
    pub fn pending(&self) -> &[u8] {
        &self.buffer
    }

    /// # Errors
    /// When the declared length is impossible or over [`MAX_STARTUP_PACKET`].
    pub fn next_startup(&mut self) -> Result<Option<Vec<u8>>, FrameError> {
        let Some(declared) = be_i32(&self.buffer, 0) else {
            return Ok(None);
        };
        let length = usize::try_from(declared).map_err(|_| FrameError::BadLength(declared))?;
        if length < 8 {
            return Err(FrameError::BadLength(declared));
        }
        if length > MAX_STARTUP_PACKET {
            return Err(FrameError::TooLong(length));
        }
        Ok((self.buffer.len() >= length).then(|| self.buffer.drain(..length).collect()))
    }

    /// # Errors
    /// When the declared length is impossible or over [`MAX_MESSAGE`].
    pub fn next_message(&mut self) -> Result<Option<Vec<u8>>, FrameError> {
        let Some(declared) = be_i32(&self.buffer, 1) else {
            return Ok(None);
        };
        let length = usize::try_from(declared).map_err(|_| FrameError::BadLength(declared))?;
        if length < 4 {
            return Err(FrameError::BadLength(declared));
        }
        let total = length + 1;
        if total > MAX_MESSAGE {
            return Err(FrameError::TooLong(total));
        }
        Ok((self.buffer.len() >= total).then(|| self.buffer.drain(..total).collect()))
    }

    /// Every complete typed message buffered so far.
    ///
    /// # Errors
    /// When a frame header is invalid.
    pub fn drain_messages(&mut self) -> Result<Vec<Vec<u8>>, FrameError> {
        let mut out = Vec::new();
        while let Some(frame) = self.next_message()? {
            out.push(frame);
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn packet(code: i32, rest: &[u8]) -> Vec<u8> {
        let length = i32::try_from(8 + rest.len()).unwrap_or(i32::MAX);
        let mut out = length.to_be_bytes().to_vec();
        out.extend_from_slice(&code.to_be_bytes());
        out.extend_from_slice(rest);
        out
    }

    #[test]
    fn an_ssl_request_is_recognised() {
        assert_eq!(
            classify_startup(&packet(SSL_REQUEST_CODE, &[])),
            Ok(Startup::Ssl)
        );
        assert_eq!(
            classify_startup(&packet(GSSENC_REQUEST_CODE, &[])),
            Ok(Startup::Gss)
        );
    }

    #[test]
    fn a_cancel_request_is_recognised() {
        let cancel = packet(CANCEL_REQUEST_CODE, &[0, 0, 0, 7, 0, 0, 0, 9]);
        assert_eq!(classify_startup(&cancel), Ok(Startup::Cancel));
    }

    #[test]
    fn a_v3_startup_is_recognised_and_decoded() {
        let startup = packet(
            PROTOCOL_3,
            b"user\0postgres\0database\0postgres\0options\0-c a=b\0\0",
        );
        assert_eq!(classify_startup(&startup), Ok(Startup::V3));
        let parameters = startup_parameters(&startup);
        assert_eq!(
            parameters.as_ref().map(|p| p.get("user").copied()),
            Ok(Some("postgres"))
        );
        assert_eq!(
            parameters.map(|p| p.get("options").copied()),
            Ok(Some("-c a=b"))
        );
    }

    #[test]
    fn a_newer_minor_version_is_recognised_for_negotiation() {
        let newer = packet(PROTOCOL_3 + 2, b"user\0postgres\0\0");
        assert_eq!(classify_startup(&newer), Ok(Startup::V3Newer(2)));
        assert_eq!(
            startup_parameters(&newer).map(|p| p.get("user").copied()),
            Ok(Some("postgres"))
        );
        // another major version is not protocol 3 at all
        assert_eq!(
            classify_startup(&packet(4 << 16, &[])),
            Err(FrameError::UnsupportedCode(4 << 16))
        );
    }

    #[test]
    fn garbage_is_rejected() {
        assert_eq!(classify_startup(&[]), Err(FrameError::TooShort(0)));
        assert_eq!(
            classify_startup(&[0, 0, 0, 8]),
            Err(FrameError::TooShort(4))
        );
        // a declared length that disagrees with the bytes
        let mut wrong = packet(PROTOCOL_3, &[0]);
        wrong[3] = 40;
        assert_eq!(classify_startup(&wrong), Err(FrameError::BadLength(40)));
        assert_eq!(
            classify_startup(&packet(0x0102_0304, &[])),
            Err(FrameError::UnsupportedCode(0x0102_0304))
        );
        // plain HTTP text on the socket
        assert!(classify_startup(b"GET / HTTP/1.1\r\n").is_err());
    }

    #[test]
    fn startup_parameters_reject_duplicates_and_trailing_bytes() {
        let duplicate = packet(PROTOCOL_3, b"user\0a\0user\0b\0\0");
        assert!(startup_parameters(&duplicate).is_err());
        let trailing = packet(PROTOCOL_3, b"user\0a\0\0junk\0");
        assert!(startup_parameters(&trailing).is_err());
        let unterminated = packet(PROTOCOL_3, b"user\0a\0");
        assert!(startup_parameters(&unterminated).is_err());
        assert!(startup_parameters(&packet(SSL_REQUEST_CODE, &[])).is_err());
    }

    #[test]
    fn the_reader_splits_startup_packets_across_pushes() {
        let mut reader = FrameReader::default();
        let ssl = packet(SSL_REQUEST_CODE, &[]);
        reader.push(&ssl[..3]);
        assert_eq!(reader.next_startup(), Ok(None));
        reader.push(&ssl[3..]);
        assert_eq!(reader.next_startup(), Ok(Some(ssl)));
        assert_eq!(reader.next_startup(), Ok(None));
    }

    #[test]
    fn the_reader_refuses_oversized_and_undersized_startup_packets() {
        let mut oversized = FrameReader::default();
        oversized.push(&20_000_i32.to_be_bytes());
        assert_eq!(oversized.next_startup(), Err(FrameError::TooLong(20_000)));
        let mut undersized = FrameReader::default();
        undersized.push(&3_i32.to_be_bytes());
        assert_eq!(undersized.next_startup(), Err(FrameError::BadLength(3)));
    }

    #[test]
    fn the_reader_splits_typed_messages() {
        let mut reader = FrameReader::default();
        let query = [b"Q".as_slice(), &9_i32.to_be_bytes(), b"ab;\0\0".as_slice()].concat();
        let sync = [b"S".as_slice(), &4_i32.to_be_bytes()].concat();
        reader.push(&query);
        reader.push(&sync);
        reader.push(&sync[..2]);
        assert_eq!(reader.drain_messages(), Ok(vec![query, sync.clone()]));
        assert_eq!(reader.pending(), &sync[..2]);
        let mut bad = FrameReader::default();
        bad.push(&[b'Q', 0, 0, 0, 2]);
        assert_eq!(bad.next_message(), Err(FrameError::BadLength(2)));
    }
}

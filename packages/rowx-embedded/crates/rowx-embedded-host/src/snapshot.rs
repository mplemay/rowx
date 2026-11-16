// the self-describing snapshot container: MAGIC | u32 BE header length | JSON header | engine backup bytes

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

// bump when the container layout or the way a snapshot is restored changes
pub const SNAPSHOT_FORMAT: u32 = 1;
const MAGIC: &[u8; 8] = b"ROWXSNAP";
const MAX_HEADER: usize = 64 * 1024;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Identity {
    pub engine_version: String,
    pub runtime_version: String,
    pub wasm_sha256: String,
    pub extensions: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Header {
    pub format: u32,
    #[serde(flatten)]
    pub identity: Identity,
    pub payload_len: u64,
    pub payload_sha256: String,
}

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum SnapshotError {
    #[error("not a rowx-embedded snapshot (bad magic)")]
    NotASnapshot,
    #[error("the snapshot is truncated")]
    Truncated,
    #[error("the snapshot header is unreadable: {0}")]
    BadHeader(String),
    #[error(
        "the snapshot cannot be used by this build: its {what} is {snapshot} but this build has {runtime}; take a new snapshot"
    )]
    Mismatch {
        what: &'static str,
        snapshot: String,
        runtime: String,
    },
    #[error("the snapshot payload does not match its recorded digest")]
    Corrupt,
}

#[must_use]
pub fn sha256_hex(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

/// Wraps an engine backup into a snapshot.
///
/// # Errors
/// When the header cannot be serialised.
pub fn encode(identity: &Identity, payload: &[u8]) -> Result<Vec<u8>, SnapshotError> {
    let header = Header {
        format: SNAPSHOT_FORMAT,
        identity: identity.clone(),
        payload_len: payload.len() as u64,
        payload_sha256: sha256_hex(payload),
    };
    let json =
        serde_json::to_vec(&header).map_err(|error| SnapshotError::BadHeader(error.to_string()))?;
    let json_len = u32::try_from(json.len())
        .map_err(|_| SnapshotError::BadHeader("header too large".into()))?;
    let mut out = Vec::with_capacity(MAGIC.len() + 4 + json.len() + payload.len());
    out.extend_from_slice(MAGIC);
    out.extend_from_slice(&json_len.to_be_bytes());
    out.extend_from_slice(&json);
    out.extend_from_slice(payload);
    Ok(out)
}

/// Splits a snapshot into its header and the engine backup, without hashing the payload.
///
/// # Errors
/// When the bytes are not a snapshot, are truncated, or were written by another snapshot format.
pub fn decode(bytes: &[u8]) -> Result<(Header, &[u8]), SnapshotError> {
    let rest = bytes
        .strip_prefix(MAGIC.as_slice())
        .ok_or(SnapshotError::NotASnapshot)?;
    let (length, rest) = rest
        .split_first_chunk::<4>()
        .ok_or(SnapshotError::Truncated)?;
    let length = u32::from_be_bytes(*length) as usize;
    if length > MAX_HEADER {
        return Err(SnapshotError::BadHeader(format!(
            "header of {length} bytes"
        )));
    }
    let json = rest.get(..length).ok_or(SnapshotError::Truncated)?;
    let payload = rest.get(length..).ok_or(SnapshotError::Truncated)?;
    let value: serde_json::Value = serde_json::from_slice(json)
        .map_err(|error| SnapshotError::BadHeader(error.to_string()))?;
    // the format is read before the rest, so a future layout reports a clear mismatch instead of a parse error
    let format = value
        .get("format")
        .and_then(serde_json::Value::as_u64)
        .ok_or_else(|| SnapshotError::BadHeader("missing format".into()))?;
    if format != u64::from(SNAPSHOT_FORMAT) {
        return Err(SnapshotError::Mismatch {
            what: "snapshot format",
            snapshot: format.to_string(),
            runtime: SNAPSHOT_FORMAT.to_string(),
        });
    }
    let header: Header = serde_json::from_value(value)
        .map_err(|error| SnapshotError::BadHeader(error.to_string()))?;
    if header.payload_len != payload.len() as u64 {
        return Err(SnapshotError::Truncated);
    }
    Ok((header, payload))
}

impl Header {
    /// # Errors
    /// Names the first identity field that differs from this build.
    pub fn check(&self, current: &Identity) -> Result<(), SnapshotError> {
        let mismatch =
            |what: &'static str, snapshot: &str, runtime: &str| SnapshotError::Mismatch {
                what,
                snapshot: snapshot.to_owned(),
                runtime: runtime.to_owned(),
            };
        let ours = &self.identity;
        if ours.engine_version != current.engine_version {
            return Err(mismatch(
                "engine version",
                &ours.engine_version,
                &current.engine_version,
            ));
        }
        if ours.runtime_version != current.runtime_version {
            return Err(mismatch(
                "runtime version",
                &ours.runtime_version,
                &current.runtime_version,
            ));
        }
        if ours.wasm_sha256 != current.wasm_sha256 {
            return Err(mismatch(
                "wasm hash",
                &ours.wasm_sha256,
                &current.wasm_sha256,
            ));
        }
        if ours.extensions != current.extensions {
            return Err(mismatch(
                "extension set",
                &ours.extensions.join(","),
                &current.extensions.join(","),
            ));
        }
        Ok(())
    }

    /// # Errors
    /// When the payload does not hash to the recorded digest.
    pub fn verify_payload(&self, payload: &[u8]) -> Result<(), SnapshotError> {
        if sha256_hex(payload).eq_ignore_ascii_case(&self.payload_sha256) {
            Ok(())
        } else {
            Err(SnapshotError::Corrupt)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn identity() -> Identity {
        Identity {
            engine_version: "18.4".into(),
            runtime_version: "0.3.2".into(),
            wasm_sha256: "ab".repeat(32),
            extensions: vec!["citext".into(), "hstore".into()],
        }
    }

    #[test]
    fn a_snapshot_round_trips() -> Result<(), SnapshotError> {
        let bytes = encode(&identity(), b"backup bytes")?;
        let (header, payload) = decode(&bytes)?;
        assert_eq!(payload, b"backup bytes");
        assert_eq!(header.identity, identity());
        assert_eq!(header.format, SNAPSHOT_FORMAT);
        header.check(&identity())?;
        header.verify_payload(payload)
    }

    #[test]
    fn foreign_and_truncated_bytes_are_rejected() -> Result<(), SnapshotError> {
        assert_eq!(
            decode(b"PK\x03\x04 not ours").err(),
            Some(SnapshotError::NotASnapshot)
        );
        let bytes = encode(&identity(), b"backup bytes")?;
        assert_eq!(decode(&bytes[..10]).err(), Some(SnapshotError::Truncated));
        assert_eq!(
            decode(&bytes[..bytes.len() - 1]).err(),
            Some(SnapshotError::Truncated)
        );
        Ok(())
    }

    #[test]
    fn every_identity_field_mismatch_is_named() -> Result<(), SnapshotError> {
        let bytes = encode(&identity(), b"x")?;
        let (header, _) = decode(&bytes)?;
        for (what, change) in [
            (
                "engine version",
                Identity {
                    engine_version: "18.5".into(),
                    ..identity()
                },
            ),
            (
                "runtime version",
                Identity {
                    runtime_version: "0.4.0".into(),
                    ..identity()
                },
            ),
            (
                "wasm hash",
                Identity {
                    wasm_sha256: "cd".repeat(32),
                    ..identity()
                },
            ),
            (
                "extension set",
                Identity {
                    extensions: vec!["citext".into()],
                    ..identity()
                },
            ),
        ] {
            let error = header
                .check(&change)
                .err()
                .map(|e| e.to_string())
                .unwrap_or_default();
            assert!(error.contains(what), "{error}");
            assert!(error.contains("take a new snapshot"), "{error}");
        }
        Ok(())
    }

    #[test]
    fn another_format_version_is_a_clear_mismatch() {
        let json = br#"{"format":99,"anything":"else"}"#;
        let mut bytes = MAGIC.to_vec();
        bytes.extend_from_slice(&u32::try_from(json.len()).unwrap_or(0).to_be_bytes());
        bytes.extend_from_slice(json);
        let error = decode(&bytes)
            .err()
            .map(|e| e.to_string())
            .unwrap_or_default();
        assert!(error.contains("snapshot format is 99"), "{error}");
    }

    #[test]
    fn a_corrupted_payload_fails_verification() -> Result<(), SnapshotError> {
        let bytes = encode(&identity(), b"abc")?;
        let (header, _) = decode(&bytes)?;
        assert_eq!(header.verify_payload(b"abd"), Err(SnapshotError::Corrupt));
        Ok(())
    }
}

// identity of the engine this build embeds; every snapshot records it

use std::sync::OnceLock;

use oliphaunt_wasix::Extension;

use crate::error::{HostError, Result};

// the PostgreSQL release the wasm engine implements (`server_version`)
pub const ENGINE_VERSION: &str = "18.4";

// every contrib extension the engine ships, enabled at build time and part of every snapshot's identity
pub const EXTENSIONS: [&str; 32] = [
    "amcheck",
    "auto_explain",
    "bloom",
    "btree_gin",
    "btree_gist",
    "citext",
    "cube",
    "dict_int",
    "dict_xsyn",
    "earthdistance",
    "file_fdw",
    "fuzzystrmatch",
    "hstore",
    "intarray",
    "isn",
    "lo",
    "ltree",
    "pageinspect",
    "pg_buffercache",
    "pg_freespacemap",
    "pg_surgery",
    "pg_trgm",
    "pg_visibility",
    "pg_walinspect",
    "pgcrypto",
    "seg",
    "tablefunc",
    "tcn",
    "tsm_system_rows",
    "tsm_system_time",
    "unaccent",
    "uuid-ossp",
];

// memory and temporary instances trade durability for speed: a lost process loses the instance anyway.
// A persistent directory instance keeps the engine defaults (fsync, full_page_writes and synchronous_commit on).
pub const FAST_GUCS: [(&str, &str); 3] = [
    ("fsync", "off"),
    ("full_page_writes", "off"),
    ("synchronous_commit", "off"),
];

#[must_use]
pub fn extensions() -> Vec<Extension> {
    EXTENSIONS
        .iter()
        .filter_map(|name| Extension::by_sql_name(name))
        .collect()
}

#[must_use]
pub const fn runtime_version() -> &'static str {
    liboliphaunt_wasix_portable::PACKAGE_VERSION
}

/// SHA-256 of the wasm module the engine runs; a cluster seed must name the same digest.
///
/// # Errors
/// When the packaged asset manifest cannot be parsed.
pub fn wasm_sha256() -> Result<&'static str> {
    static DIGEST: OnceLock<std::result::Result<String, String>> = OnceLock::new();
    DIGEST
        .get_or_init(|| {
            liboliphaunt_wasix_portable::manifest()
                .map(|manifest| manifest.runtime.module_sha256)
                .map_err(|error| format!("cannot read the engine asset manifest: {error}"))
        })
        .as_ref()
        .map(String::as_str)
        .map_err(|message| HostError::Engine(message.clone()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_listed_extension_resolves_in_the_engine() {
        assert_eq!(extensions().len(), EXTENSIONS.len());
    }

    #[test]
    fn the_extension_list_is_sorted_and_unique() {
        let mut sorted = EXTENSIONS;
        sorted.sort_unstable();
        sorted
            .windows(2)
            .for_each(|pair| assert!(pair[0] < pair[1]));
        assert_eq!(sorted, EXTENSIONS);
    }

    #[test]
    fn the_wasm_digest_is_a_sha256_hex_string() {
        let digest = wasm_sha256().map(str::to_owned);
        assert!(digest.is_ok_and(|d| d.len() == 64 && d.bytes().all(|b| b.is_ascii_hexdigit())));
    }
}

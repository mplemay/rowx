// A snapshot is an engine backup, which needs crash recovery on every boot (about 1 s). The fast path derives a
// cluster seed from it once per process: restore into a private directory, open and close it cleanly, and pack the
// resulting pgdata. A memory database then boots from that seed in about 100 ms.
//
// The seed manifest is built by hand: the engine's published seed is bound to an older runtime and the format is not
// documented, so this is pinned to `oliphaunt-wasix =0.3.2`. Any failure falls back to the documented directory
// restore (see `instance.rs`).

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use oliphaunt_wasix::{DatabaseStorage, Oliphaunt};
use serde_json::json;

use crate::error::{HostError, Result};
use crate::runtime;
use crate::snapshot::{Header, sha256_hex};

// derived seeds kept per process (each is a few MB; the engine keeps its own unpacked copy of each)
const CACHE_LIMIT: usize = 4;
const ARCHIVE_NAME: &str = "rowx-embedded-seed.tar.zst";
const ZSTD_LEVEL: i32 = 3;
// runtime state a clean pgdata must not carry into a new cluster
const EXCLUDED: [&str; 2] = ["postmaster.pid", "backup_label.old"];

#[derive(Debug)]
pub struct Seed {
    pub archive: Arc<[u8]>,
    pub manifest: Vec<u8>,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct ArchiveStats {
    pub regular_files: u64,
    pub directories: u64,
    pub expanded_bytes: u64,
}

/// The `oliphaunt-cluster-seed-v1` manifest for `archive`.
#[must_use]
pub fn manifest_json(archive: &[u8], stats: ArchiveStats, wasm_sha256: &str) -> Vec<u8> {
    let manifest = json!({
        "schema": "oliphaunt-cluster-seed-v1",
        "artifactRole": "cluster-seed-standard",
        "catalogProfile": "standard",
        "runtime": {
            "product": "liboliphaunt-wasix",
            "version": runtime::runtime_version(),
            "engineFamily": "wasix",
            "target": "portable",
            "physicalFormat": liboliphaunt_wasix_portable::PHYSICAL_FORMAT,
            "postgresMajor": liboliphaunt_wasix_portable::POSTGRES_MAJOR,
            "compatibilityKey": "wasix-pg18-datum32-v1",
            "consumerSha256": wasm_sha256,
            "producerSha256": wasm_sha256,
            "initdbSha256": wasm_sha256,
        },
        "source": { "producer": "wasix-initdb" },
        "archive": {
            "path": ARCHIVE_NAME,
            "sha256": sha256_hex(archive),
            "compressedBytes": archive.len(),
            "expandedBytes": stats.expanded_bytes,
            "regularFiles": stats.regular_files,
            "directories": stats.directories,
        },
        "requiredRuntimeFeatures": [],
        "extensions": { "selected": [], "startupConfiguration": [] },
        "icu": null,
    });
    manifest.to_string().into_bytes()
}

// the directory under `root` that holds a cluster (`PG_VERSION` and `global/pg_control`), searched a few levels deep
#[must_use]
pub fn find_pgdata(root: &Path) -> Option<PathBuf> {
    fn is_pgdata(dir: &Path) -> bool {
        dir.join("PG_VERSION").is_file() && dir.join("global").join("pg_control").is_file()
    }
    fn walk(dir: &Path, depth: u32) -> Option<PathBuf> {
        if is_pgdata(dir) {
            return Some(dir.to_path_buf());
        }
        if depth == 0 {
            return None;
        }
        let mut children: Vec<PathBuf> = fs::read_dir(dir)
            .ok()?
            .flatten()
            .filter(|entry| entry.file_type().is_ok_and(|kind| kind.is_dir()))
            .map(|entry| entry.path())
            .collect();
        children.sort();
        children
            .into_iter()
            .find_map(|child| walk(&child, depth - 1))
    }
    walk(root, 4)
}

fn pack_dir(
    builder: &mut tar::Builder<impl std::io::Write>,
    root: &Path,
    dir: &Path,
    stats: &mut ArchiveStats,
) -> Result<()> {
    let mut entries: Vec<_> = fs::read_dir(dir)?.collect::<std::io::Result<_>>()?;
    entries.sort_by_key(fs::DirEntry::file_name);
    for entry in entries {
        let path = entry.path();
        let relative = path
            .strip_prefix(root)
            .map_err(|_| HostError::Engine("pgdata entry outside its root".into()))?;
        if dir == root
            && relative
                .to_str()
                .is_some_and(|name| EXCLUDED.contains(&name))
        {
            continue;
        }
        let kind = entry.file_type()?;
        if kind.is_dir() {
            builder.append_dir(relative, &path)?;
            stats.directories += 1;
            pack_dir(builder, root, &path, stats)?;
        } else if kind.is_file() {
            stats.expanded_bytes += entry.metadata()?.len();
            stats.regular_files += 1;
            builder.append_path_with_name(&path, relative)?;
        } else {
            return Err(HostError::Engine(format!(
                "unexpected entry in pgdata: {}",
                path.display()
            )));
        }
    }
    Ok(())
}

/// Packs the pgdata directory `pgdata` as a zstd-compressed tar whose root is the pgdata contents.
///
/// # Errors
/// When a file cannot be read or the archive cannot be written.
pub fn pack_pgdata(pgdata: &Path) -> Result<(Vec<u8>, ArchiveStats)> {
    let encoder = zstd::stream::write::Encoder::new(Vec::new(), ZSTD_LEVEL)?;
    let mut builder = tar::Builder::new(encoder);
    let mut stats = ArchiveStats::default();
    pack_dir(&mut builder, pgdata, pgdata, &mut stats)?;
    let archive = builder.into_inner()?.finish()?;
    Ok((archive, stats))
}

// restore, open and close once with the fast GUCs, then pack. The caller owns `work` and removes it.
fn derive(payload: &[u8], work: &Path) -> Result<Seed> {
    let db_dir = work.join("db");
    Oliphaunt::restore(&db_dir, payload)?;
    let mut db = Oliphaunt::builder()
        .extensions(runtime::extensions())
        .startup_gucs(
            runtime::FAST_GUCS
                .iter()
                .map(|(k, v)| ((*k).to_owned(), (*v).to_owned())),
        )
        .storage(DatabaseStorage::Directory(db_dir.clone()))
        .open()?;
    db.close()?;
    let pgdata = find_pgdata(&db_dir)
        .ok_or_else(|| HostError::Engine("the restored cluster has no PG_VERSION".into()))?;
    let (archive, stats) = pack_pgdata(&pgdata)?;
    let manifest = manifest_json(&archive, stats, runtime::wasm_sha256()?);
    Ok(Seed {
        archive: Arc::from(archive),
        manifest,
    })
}

type Cache = Vec<(String, Arc<Seed>)>;

fn cache() -> &'static Mutex<Cache> {
    static CACHE: Mutex<Cache> = Mutex::new(Vec::new());
    &CACHE
}

/// The cached seed for a snapshot, if this process has derived one.
#[must_use]
pub fn cached(header: &Header) -> Option<Arc<Seed>> {
    let cache = cache().lock().ok()?;
    cache
        .iter()
        .find(|(key, _)| *key == header.payload_sha256)
        .map(|(_, seed)| Arc::clone(seed))
}

/// Derives (and caches) the seed for a snapshot. The caller has verified the payload against the header's digest.
/// Derivations are serialised, so concurrent starts from one snapshot derive it once.
///
/// # Errors
/// When the payload is corrupt or any engine step fails.
pub fn derive_cached(header: &Header, payload: &[u8], work: &Path) -> Result<Arc<Seed>> {
    static DERIVING: Mutex<()> = Mutex::new(());
    let serial = DERIVING.lock().map_err(|_| HostError::EngineGone)?;
    if let Some(seed) = cached(header) {
        return Ok(seed);
    }
    fs::create_dir_all(work)?;
    // a short-lived thread with a roomy stack, independent of whichever thread the caller is on
    let seed = std::thread::scope(|scope| {
        std::thread::Builder::new()
            .name("rowx-seed".into())
            .stack_size(8 * 1024 * 1024)
            .spawn_scoped(scope, || derive(payload, work))
            .map_err(HostError::Io)?
            .join()
            .map_err(|_| HostError::Engine("the seed derivation panicked".into()))?
    });
    let _ = fs::remove_dir_all(work);
    let seed = Arc::new(seed?);
    remember(header, &seed);
    drop(serial);
    Ok(seed)
}

fn remember(header: &Header, seed: &Arc<Seed>) {
    if let Ok(mut cache) = cache().lock() {
        if cache.len() >= CACHE_LIMIT {
            cache.remove(0);
        }
        cache.push((header.payload_sha256.clone(), Arc::clone(seed)));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Read;

    #[test]
    fn the_manifest_carries_the_archive_identity_and_the_runtime()
    -> std::result::Result<(), serde_json::Error> {
        let stats = ArchiveStats {
            regular_files: 3,
            directories: 2,
            expanded_bytes: 99,
        };
        let manifest: serde_json::Value =
            serde_json::from_slice(&manifest_json(b"archive", stats, "ab"))?;
        assert_eq!(manifest["schema"], "oliphaunt-cluster-seed-v1");
        assert_eq!(manifest["runtime"]["version"], runtime::runtime_version());
        assert_eq!(manifest["runtime"]["consumerSha256"], "ab");
        assert_eq!(
            manifest["runtime"]["producerSha256"],
            manifest["runtime"]["consumerSha256"]
        );
        assert_eq!(manifest["archive"]["sha256"], sha256_hex(b"archive"));
        assert_eq!(manifest["archive"]["compressedBytes"], 7);
        assert_eq!(manifest["archive"]["regularFiles"], 3);
        assert_eq!(manifest["extensions"]["selected"], json!([]));
        Ok(())
    }

    #[test]
    fn pgdata_is_packed_without_runtime_state() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let pgdata = dir.path().join("db").join("pgdata");
        fs::create_dir_all(pgdata.join("global"))?;
        fs::write(pgdata.join("PG_VERSION"), "18\n")?;
        fs::write(pgdata.join("global").join("pg_control"), "control")?;
        fs::write(pgdata.join("postmaster.pid"), "123")?;
        fs::write(pgdata.join("backup_label.old"), "old")?;
        assert_eq!(find_pgdata(dir.path()), Some(pgdata.clone()));

        let (archive, stats) = pack_pgdata(&pgdata)?;
        assert_eq!(stats.regular_files, 2);
        assert_eq!(stats.directories, 1);
        let mut names = Vec::new();
        let mut reader = tar::Archive::new(zstd::stream::read::Decoder::new(archive.as_slice())?);
        for entry in reader.entries()? {
            let mut entry = entry?;
            let mut body = String::new();
            let _ = entry.read_to_string(&mut body);
            names.push(entry.path()?.to_string_lossy().into_owned());
        }
        names.sort();
        assert_eq!(names, ["PG_VERSION", "global", "global/pg_control"]);
        Ok(())
    }

    #[test]
    fn a_directory_without_a_cluster_has_no_pgdata() -> std::io::Result<()> {
        let dir = tempfile::tempdir()?;
        fs::create_dir_all(dir.path().join("a").join("b"))?;
        assert_eq!(find_pgdata(dir.path()), None);
        Ok(())
    }
}

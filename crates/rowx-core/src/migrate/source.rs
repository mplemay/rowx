use std::collections::BTreeMap;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use thiserror::Error;

use super::checksum::checksum;
use super::migration::{Migration, MigrationKind};

const NO_TRANSACTION: &[u8] = b"-- no-transaction";

#[derive(Debug, Error)]
pub enum SourceError {
    #[error("cannot read migration directory {}: {source}", dir.display())]
    ReadDir { dir: PathBuf, source: io::Error },
    #[error("cannot read migration file {}: {source}", path.display())]
    ReadFile { path: PathBuf, source: io::Error },
    #[error("migration file {} is not a valid name: {reason}", path.display())]
    InvalidName { path: PathBuf, reason: &'static str },
    #[error("migration file {} is not valid UTF-8", path.display())]
    NotUtf8 { path: PathBuf },
    #[error(
        "migration version {version} is defined twice: {} and {}",
        first.display(),
        second.display()
    )]
    Duplicate {
        version: i64,
        first: PathBuf,
        second: PathBuf,
    },
    #[error(
        "migration version {version} mixes a simple and a reversible migration: {} and {}",
        first.display(),
        second.display()
    )]
    MixedKinds {
        version: i64,
        first: PathBuf,
        second: PathBuf,
    },
    #[error("migration file {} has no matching .down.sql file", path.display())]
    UnpairedUp { version: i64, path: PathBuf },
    #[error("migration file {} has no matching .up.sql file", path.display())]
    UnpairedDown { version: i64, path: PathBuf },
}

impl SourceError {
    // The file the error is about; for a pair of files, the one that came second
    #[must_use]
    pub fn path(&self) -> &Path {
        match self {
            Self::ReadDir { dir, .. } => dir,
            Self::ReadFile { path, .. }
            | Self::InvalidName { path, .. }
            | Self::NotUtf8 { path }
            | Self::UnpairedUp { path, .. }
            | Self::UnpairedDown { path, .. } => path,
            Self::Duplicate { second, .. } | Self::MixedKinds { second, .. } => second,
        }
    }

    #[must_use]
    pub const fn version(&self) -> Option<i64> {
        match self {
            Self::Duplicate { version, .. }
            | Self::MixedKinds { version, .. }
            | Self::UnpairedUp { version, .. }
            | Self::UnpairedDown { version, .. } => Some(*version),
            Self::ReadDir { .. }
            | Self::ReadFile { .. }
            | Self::InvalidName { .. }
            | Self::NotUtf8 { .. } => None,
        }
    }
}

// Reads the regular `.sql` files directly in `dir` and returns them sorted by (version, kind)
///
/// # Errors
///
/// Returns a [`SourceError`] naming the file for an unreadable, badly named or non-UTF-8
/// file, a duplicate version, or an unpaired `.up.sql` or `.down.sql`.
pub fn resolve(dir: &Path) -> Result<Vec<Migration>, SourceError> {
    let read_dir_error = |source| SourceError::ReadDir {
        dir: dir.to_path_buf(),
        source,
    };
    let mut files = Vec::new();
    for entry in fs::read_dir(dir).map_err(read_dir_error)? {
        let entry = entry.map_err(read_dir_error)?;
        let name = entry.file_name();
        // Filter on the name before touching the file, so an ignored entry (a dangling
        // `README.md` symlink, an unreadable `.gitkeep`) can never fail resolution
        if !name.to_string_lossy().ends_with(".sql") {
            continue;
        }
        let path = entry.path();
        // `fs::metadata` follows symlinks, so a link to a file counts as a file
        let metadata = fs::metadata(&path).map_err(|source| SourceError::ReadFile {
            path: path.clone(),
            source,
        })?;
        if metadata.is_file() {
            files.push((name, path));
        }
    }
    // Directory order is arbitrary; sorting makes the first error deterministic
    files.sort();

    let mut migrations = Vec::new();
    for (name, path) in files {
        let Some(name) = name.to_str() else {
            return Err(SourceError::InvalidName {
                path,
                reason: "the file name is not valid UTF-8",
            });
        };
        let (version, description, kind) =
            parse_name(name).map_err(|reason| SourceError::InvalidName {
                path: path.clone(),
                reason,
            })?;
        let bytes = fs::read(&path).map_err(|source| SourceError::ReadFile {
            path: path.clone(),
            source,
        })?;
        let checksum = checksum(&bytes);
        let sql =
            String::from_utf8(bytes).map_err(|_| SourceError::NotUtf8 { path: path.clone() })?;
        migrations.push(Migration {
            version,
            description,
            kind,
            // sqlx matches the prefix, so `-- no-transaction: because ...` also opts out
            no_transaction: sql.as_bytes().starts_with(NO_TRANSACTION),
            sql,
            checksum,
            path,
        });
    }
    migrations.sort();
    validate(&migrations)?;
    Ok(migrations)
}

// `<VERSION>_<DESCRIPTION>` + `.sql` | `.up.sql` | `.down.sql`
fn parse_name(name: &str) -> Result<(i64, String, MigrationKind), &'static str> {
    let (stem, kind) = MigrationKind::from_file_name(name).ok_or("it does not end in .sql")?;
    let (version, description) = stem
        .split_once('_')
        .ok_or("expected <VERSION>_<DESCRIPTION>.sql")?;
    if version.is_empty() || !version.bytes().all(|b| b.is_ascii_digit()) {
        return Err("the version must be a non-negative integer");
    }
    // leading zeros are fine: `001` is version 1
    let version: i64 = version
        .parse()
        .map_err(|_| "the version does not fit in a 64-bit integer")?;
    Ok((version, description.replace('_', " "), kind))
}

// `migrations` is sorted, so all files of one version are adjacent
fn validate(migrations: &[Migration]) -> Result<(), SourceError> {
    let mut by_version: BTreeMap<i64, Vec<&Migration>> = BTreeMap::new();
    for migration in migrations {
        by_version
            .entry(migration.version)
            .or_default()
            .push(migration);
    }
    for (&version, group) in &by_version {
        for pair in group.windows(2) {
            let (first, second) = (pair[0], pair[1]);
            let (first_path, second_path) = (first.path.clone(), second.path.clone());
            if first.kind == second.kind {
                return Err(SourceError::Duplicate {
                    version,
                    first: first_path,
                    second: second_path,
                });
            }
            if (first.kind == MigrationKind::Simple) != (second.kind == MigrationKind::Simple) {
                return Err(SourceError::MixedKinds {
                    version,
                    first: first_path,
                    second: second_path,
                });
            }
        }
        let up = group.iter().find(|m| m.kind == MigrationKind::Up);
        let down = group.iter().find(|m| m.kind == MigrationKind::Down);
        match (up, down) {
            (Some(up), None) => {
                return Err(SourceError::UnpairedUp {
                    version,
                    path: up.path.clone(),
                });
            }
            (None, Some(down)) => {
                return Err(SourceError::UnpairedDown {
                    version,
                    path: down.path.clone(),
                });
            }
            _ => {}
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::path::Path;

    use tempfile::TempDir;

    use super::{SourceError, parse_name, resolve};
    use crate::migrate::{MigrationKind, checksum};

    fn dir_with(files: &[(&str, &[u8])]) -> TempDir {
        let dir = TempDir::new().unwrap_or_else(|e| unreachable!("tempdir: {e}"));
        for (name, content) in files {
            fs::write(dir.path().join(name), content).unwrap_or_else(|e| unreachable!("{e}"));
        }
        dir
    }

    fn names(dir: &Path) -> Vec<(i64, MigrationKind, String)> {
        resolve(dir)
            .unwrap_or_else(|e| unreachable!("{e}"))
            .into_iter()
            .map(|m| (m.version, m.kind, m.description))
            .collect()
    }

    fn error_for(files: &[(&str, &[u8])]) -> SourceError {
        let dir = dir_with(files);
        resolve(dir.path())
            .err()
            .unwrap_or_else(|| unreachable!("resolution should fail"))
    }

    fn file_name(error: &SourceError) -> String {
        error
            .path()
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default()
    }

    #[test]
    fn parses_simple_names() {
        assert_eq!(
            parse_name("20261029120000_create_users.sql"),
            Ok((
                20_261_029_120_000,
                "create users".to_owned(),
                MigrationKind::Simple
            ))
        );
        assert_eq!(
            parse_name("01_foo.sql"),
            Ok((1, "foo".to_owned(), MigrationKind::Simple))
        );
    }

    #[test]
    fn parses_reversible_names() {
        assert_eq!(
            parse_name("7_add_email_index.up.sql"),
            Ok((7, "add email index".to_owned(), MigrationKind::Up))
        );
        assert_eq!(
            parse_name("7_add_email_index.down.sql"),
            Ok((7, "add email index".to_owned(), MigrationKind::Down))
        );
    }

    #[test]
    fn only_the_first_underscore_splits_and_the_rest_become_spaces() {
        assert_eq!(
            parse_name("3_a__b_c.sql"),
            Ok((3, "a  b c".to_owned(), MigrationKind::Simple))
        );
        assert_eq!(
            parse_name("3_.sql"),
            Ok((3, String::new(), MigrationKind::Simple))
        );
    }

    #[test]
    fn rejects_malformed_names() {
        for name in [
            "users.sql",
            "1.sql",
            "1.up.sql",
            "_users.sql",
            "abc_users.sql",
            "-1_users.sql",
            "+1_users.sql",
            "1 2_users.sql",
            "9223372036854775808_users.sql",
            ".sql",
        ] {
            assert!(parse_name(name).is_err(), "{name} should be rejected");
        }
    }

    #[test]
    fn accepts_the_largest_i64_version() {
        assert_eq!(
            parse_name("9223372036854775807_max.sql").map(|(v, ..)| v),
            Ok(i64::MAX)
        );
    }

    #[test]
    fn resolves_a_mixed_directory_sorted() {
        let dir = dir_with(&[
            ("20_third.sql", b"select 3;"),
            ("10_second.down.sql", b"drop table b;"),
            ("10_second.up.sql", b"create table b ();"),
            ("2_first.sql", b"select 1;"),
        ]);
        assert_eq!(
            names(dir.path()),
            [
                (2, MigrationKind::Simple, "first".to_owned()),
                (10, MigrationKind::Up, "second".to_owned()),
                (10, MigrationKind::Down, "second".to_owned()),
                (20, MigrationKind::Simple, "third".to_owned()),
            ]
        );
    }

    #[test]
    fn carries_sql_checksum_and_path() {
        let dir = dir_with(&[("1_a.sql", b"select 1;\r\n")]);
        let resolved = resolve(dir.path()).unwrap_or_else(|e| unreachable!("{e}"));
        let [migration] = resolved.as_slice() else {
            unreachable!("one migration")
        };
        assert_eq!(migration.sql, "select 1;\r\n");
        assert_eq!(migration.checksum, checksum(b"select 1;\r\n"));
        assert_eq!(migration.path, dir.path().join("1_a.sql"));
        assert!(!migration.no_transaction);
    }

    #[test]
    fn an_empty_directory_has_no_migrations() {
        let dir = dir_with(&[]);
        assert_eq!(names(dir.path()).len(), 0);
    }

    #[test]
    fn a_missing_directory_is_a_read_error() {
        let dir = dir_with(&[]);
        let error = resolve(&dir.path().join("nope"))
            .err()
            .unwrap_or_else(|| unreachable!("missing directory"));
        assert!(matches!(error, SourceError::ReadDir { .. }));
    }

    #[test]
    fn ignores_files_that_do_not_end_in_sql() {
        let dir = dir_with(&[
            ("README.md", b"docs"),
            ("notes.txt", b"x"),
            ("1_a.sql.bak", b"x"),
            ("2_b.SQL", b"x"),
            ("1_a.sql", b"select 1;"),
            (".gitkeep", b""),
        ]);
        assert_eq!(
            names(dir.path()),
            [(1, MigrationKind::Simple, "a".to_owned())]
        );
    }

    #[test]
    fn ignores_subdirectories_even_when_named_like_migrations() {
        let dir = dir_with(&[("1_a.sql", b"select 1;")]);
        let nested = dir.path().join("2_nested.sql");
        fs::create_dir(&nested).unwrap_or_else(|e| unreachable!("{e}"));
        fs::write(nested.join("3_inner.sql"), b"select 3;").unwrap_or_else(|e| unreachable!("{e}"));
        assert_eq!(
            names(dir.path()),
            [(1, MigrationKind::Simple, "a".to_owned())]
        );
    }

    #[test]
    fn a_bad_sql_name_is_an_error_naming_the_file() {
        let error = error_for(&[("1_a.sql", b""), ("oops.sql", b"")]);
        assert!(matches!(error, SourceError::InvalidName { .. }));
        assert_eq!(file_name(&error), "oops.sql");
        assert!(error.to_string().contains("oops.sql"));
    }

    #[test]
    fn version_zero_and_leading_zeros_are_accepted() {
        assert_eq!(
            parse_name("0_init.sql"),
            Ok((0, "init".to_owned(), MigrationKind::Simple))
        );
        assert_eq!(parse_name("00_init.sql").map(|(v, ..)| v), Ok(0));
        assert_eq!(parse_name("007_x.sql").map(|(v, ..)| v), Ok(7));
        let dir = dir_with(&[("0_init.sql", b"select 0;"), ("007_x.sql", b"select 7;")]);
        assert_eq!(
            names(dir.path()),
            [
                (0, MigrationKind::Simple, "init".to_owned()),
                (7, MigrationKind::Simple, "x".to_owned()),
            ]
        );
    }

    #[test]
    fn a_version_with_leading_zeros_duplicates_the_plain_one() {
        let error = error_for(&[("1_a.sql", b""), ("001_b.sql", b"")]);
        assert!(matches!(error, SourceError::Duplicate { version: 1, .. }));
        let error = error_for(&[("0_a.sql", b""), ("000_b.sql", b"")]);
        assert!(matches!(error, SourceError::Duplicate { version: 0, .. }));
    }

    #[test]
    fn duplicate_versions_of_one_kind_are_an_error() {
        let error = error_for(&[("1_a.sql", b""), ("1_b.sql", b"")]);
        assert!(matches!(error, SourceError::Duplicate { version: 1, .. }));
        let message = error.to_string();
        assert!(message.contains("1_a.sql") && message.contains("1_b.sql"));
        assert_eq!(error.version(), Some(1));

        let error = error_for(&[
            ("2_a.up.sql", b""),
            ("2_a.down.sql", b""),
            ("2_b.up.sql", b""),
        ]);
        assert!(matches!(error, SourceError::Duplicate { version: 2, .. }));
    }

    #[test]
    fn a_simple_and_a_reversible_file_cannot_share_a_version() {
        let error = error_for(&[("1_a.sql", b""), ("1_a.up.sql", b""), ("1_a.down.sql", b"")]);
        assert!(matches!(error, SourceError::MixedKinds { version: 1, .. }));
        let error = error_for(&[("1_a.sql", b""), ("1_b.down.sql", b"")]);
        assert!(matches!(error, SourceError::MixedKinds { version: 1, .. }));
    }

    #[test]
    fn an_up_without_a_down_is_an_error_naming_the_up_file() {
        let error = error_for(&[("5_a.up.sql", b"")]);
        assert!(matches!(error, SourceError::UnpairedUp { version: 5, .. }));
        assert_eq!(file_name(&error), "5_a.up.sql");
    }

    #[test]
    fn a_down_without_an_up_is_an_error_naming_the_down_file() {
        let error = error_for(&[("5_a.down.sql", b"")]);
        assert!(matches!(
            error,
            SourceError::UnpairedDown { version: 5, .. }
        ));
        assert_eq!(file_name(&error), "5_a.down.sql");
    }

    #[test]
    fn a_pair_may_differ_in_description() {
        let dir = dir_with(&[("5_a.up.sql", b""), ("5_b.down.sql", b"")]);
        assert_eq!(names(dir.path()).len(), 2);
    }

    #[test]
    fn non_utf8_content_is_an_error_naming_the_file() {
        let error = error_for(&[
            ("1_a.sql", b"select 1;"),
            ("2_bad.sql", &[0xff, 0xfe, b'x']),
        ]);
        assert!(matches!(error, SourceError::NotUtf8 { .. }));
        assert_eq!(file_name(&error), "2_bad.sql");
    }

    #[test]
    fn a_non_utf8_file_that_is_not_sql_is_ignored() {
        let dir = dir_with(&[("1_a.sql", b"select 1;"), ("blob.bin", &[0xff, 0xfe])]);
        assert_eq!(names(dir.path()).len(), 1);
    }

    #[test]
    fn a_utf8_bom_is_kept_in_the_sql_and_the_checksum() {
        let dir = dir_with(&[("1_a.sql", b"\xef\xbb\xbfselect 1;")]);
        let resolved = resolve(dir.path()).unwrap_or_else(|e| unreachable!("{e}"));
        assert_eq!(resolved[0].sql, "\u{feff}select 1;");
        assert_eq!(resolved[0].checksum, checksum(b"\xef\xbb\xbfselect 1;"));
    }

    fn no_transaction(content: &[u8]) -> bool {
        let dir = dir_with(&[("1_a.sql", content)]);
        resolve(dir.path()).unwrap_or_else(|e| unreachable!("{e}"))[0].no_transaction
    }

    #[test]
    fn no_transaction_is_detected_on_the_first_line_only() {
        assert!(no_transaction(
            b"-- no-transaction\nCREATE INDEX CONCURRENTLY i ON t (c);"
        ));
        assert!(no_transaction(b"-- no-transaction\r\nselect 1;"));
        assert!(no_transaction(b"-- no-transaction"));
        // sqlx matches the prefix of the file, so a trailing remark is allowed
        assert!(no_transaction(
            b"-- no-transaction: concurrent index\nselect 1;"
        ));
        assert!(!no_transaction(b"select 1;\n-- no-transaction\n"));
        assert!(!no_transaction(b"\n-- no-transaction\nselect 1;"));
        assert!(!no_transaction(b" -- no-transaction\nselect 1;"));
        assert!(!no_transaction(b"\xef\xbb\xbf-- no-transaction\nselect 1;"));
        assert!(!no_transaction(b"-- No-Transaction\nselect 1;"));
        assert!(!no_transaction(b"--no-transaction\nselect 1;"));
        assert!(!no_transaction(b""));
    }

    #[test]
    fn a_down_file_has_its_own_no_transaction_flag() {
        let dir = dir_with(&[
            ("1_a.up.sql", b"select 1;"),
            (
                "1_a.down.sql",
                b"-- no-transaction\ndrop index concurrently i;",
            ),
        ]);
        let resolved = resolve(dir.path()).unwrap_or_else(|e| unreachable!("{e}"));
        assert!(!resolved[0].no_transaction);
        assert!(resolved[1].no_transaction);
    }

    #[cfg(unix)]
    #[test]
    fn a_symlink_to_a_file_is_read() {
        let target = dir_with(&[("real.txt", b"select 1;")]);
        let dir = dir_with(&[]);
        std::os::unix::fs::symlink(target.path().join("real.txt"), dir.path().join("1_a.sql"))
            .unwrap_or_else(|e| unreachable!("{e}"));
        assert_eq!(names(dir.path()).len(), 1);
    }

    #[cfg(unix)]
    #[test]
    fn a_dangling_symlink_that_is_not_sql_is_ignored() {
        let dir = dir_with(&[("1_a.sql", b"select 1;")]);
        let gone = dir.path().join("gone");
        std::os::unix::fs::symlink(&gone, dir.path().join("README.md"))
            .unwrap_or_else(|e| unreachable!("{e}"));
        std::os::unix::fs::symlink(&gone, dir.path().join("2_b.sql.bak"))
            .unwrap_or_else(|e| unreachable!("{e}"));
        assert_eq!(
            names(dir.path()),
            [(1, MigrationKind::Simple, "a".to_owned())]
        );
    }

    #[cfg(unix)]
    #[test]
    fn a_dangling_sql_symlink_is_a_read_error_naming_the_file() {
        let dir = dir_with(&[("1_a.sql", b"select 1;")]);
        std::os::unix::fs::symlink(dir.path().join("gone"), dir.path().join("2_b.sql"))
            .unwrap_or_else(|e| unreachable!("{e}"));
        let error = resolve(dir.path())
            .err()
            .unwrap_or_else(|| unreachable!("dangling migration"));
        assert!(matches!(error, SourceError::ReadFile { .. }));
        assert_eq!(file_name(&error), "2_b.sql");
    }
}

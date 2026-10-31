use std::collections::BTreeMap;

use super::migration::Migration;
use super::plan::AppliedMigration;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum MigrationStatus {
    Applied,
    Pending,
    // Applied, but the local file's checksum differs from the recorded one
    Modified,
    // Applied, but there is no local file for the version
    Missing,
    // A row with `success = false`: a migration that did not finish
    Dirty,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MigrationInfo {
    pub version: i64,
    // `None` for a missing migration, because only the database knows it
    pub description: Option<String>,
    pub status: MigrationStatus,
}

// One entry per version, taken from both sides and sorted by version. Down files are
// not migrations of their own. Dirty beats missing, which beats modified.
#[must_use]
pub fn migration_info(local: &[Migration], applied: &[AppliedMigration]) -> Vec<MigrationInfo> {
    let mut forward: BTreeMap<i64, &Migration> = BTreeMap::new();
    for migration in local.iter().filter(|m| !m.kind.is_down()) {
        forward.entry(migration.version).or_insert(migration);
    }
    let applied: BTreeMap<i64, &AppliedMigration> =
        applied.iter().map(|row| (row.version, row)).collect();

    let versions: std::collections::BTreeSet<i64> =
        forward.keys().chain(applied.keys()).copied().collect();
    versions
        .into_iter()
        .map(|version| {
            let local = forward.get(&version);
            let status = match (local, applied.get(&version)) {
                (_, Some(row)) if !row.success => MigrationStatus::Dirty,
                (None, Some(_)) => MigrationStatus::Missing,
                (Some(migration), Some(row)) if migration.checksum[..] != row.checksum[..] => {
                    MigrationStatus::Modified
                }
                (_, Some(_)) => MigrationStatus::Applied,
                (_, None) => MigrationStatus::Pending,
            };
            MigrationInfo {
                version,
                description: local.map(|m| m.description.clone()),
                status,
            }
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::migrate::{MigrationKind, checksum};

    fn local(version: i64, kind: MigrationKind, sql: &str) -> Migration {
        Migration {
            version,
            description: format!("m{version}"),
            kind,
            sql: sql.to_owned(),
            checksum: checksum(sql.as_bytes()),
            no_transaction: false,
            path: std::path::PathBuf::new(),
        }
    }

    fn row(version: i64, sql: &str, success: bool) -> AppliedMigration {
        AppliedMigration {
            version,
            checksum: checksum(sql.as_bytes()).to_vec(),
            success,
        }
    }

    fn statuses(local: &[Migration], applied: &[AppliedMigration]) -> Vec<(i64, MigrationStatus)> {
        migration_info(local, applied)
            .into_iter()
            .map(|i| (i.version, i.status))
            .collect()
    }

    #[test]
    fn every_state_is_derived() {
        let migrations = [
            local(1, MigrationKind::Simple, "a"),
            local(2, MigrationKind::Simple, "b"),
            local(3, MigrationKind::Simple, "c"),
            local(4, MigrationKind::Simple, "d"),
        ];
        let applied = [
            row(1, "a", true),
            row(2, "changed", true),
            row(4, "d", false),
            row(9, "z", true),
        ];
        assert_eq!(
            statuses(&migrations, &applied),
            [
                (1, MigrationStatus::Applied),
                (2, MigrationStatus::Modified),
                (3, MigrationStatus::Pending),
                (4, MigrationStatus::Dirty),
                (9, MigrationStatus::Missing),
            ]
        );
    }

    #[test]
    fn a_dirty_row_without_a_local_file_is_dirty() {
        assert_eq!(
            statuses(&[], &[row(5, "x", false)]),
            [(5, MigrationStatus::Dirty)]
        );
    }

    #[test]
    fn an_empty_history_makes_everything_pending() {
        let migrations = [local(1, MigrationKind::Simple, "a")];
        assert_eq!(statuses(&migrations, &[]), [(1, MigrationStatus::Pending)]);
    }

    #[test]
    fn a_reversible_pair_is_one_entry_and_compares_the_up_checksum() {
        let migrations = [
            local(1, MigrationKind::Up, "up"),
            local(1, MigrationKind::Down, "down"),
        ];
        assert_eq!(
            statuses(&migrations, &[row(1, "up", true)]),
            [(1, MigrationStatus::Applied)]
        );
        assert_eq!(
            statuses(&migrations, &[row(1, "down", true)]),
            [(1, MigrationStatus::Modified)]
        );
    }

    #[test]
    fn descriptions_come_from_the_local_file() {
        let info = migration_info(
            &[local(1, MigrationKind::Simple, "a")],
            &[row(1, "a", true), row(2, "b", true)],
        );
        assert_eq!(info[0].description.as_deref(), Some("m1"));
        assert_eq!(info[1].description, None);
    }
}

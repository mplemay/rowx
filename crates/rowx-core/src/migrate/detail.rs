use std::fmt::Display;

use super::migration::Migration;
use super::plan::AppliedMigration;
use super::status::{MigrationInfo, MigrationStatus, migration_info};

// An applied row with the columns `info` reports
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AppliedRecord {
    pub migration: AppliedMigration,
    pub installed_on_micros: i64,
    pub execution_time: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MigrationDetail {
    pub info: MigrationInfo,
    // `Some` for versions that have a history row
    pub installed_on_micros: Option<i64>,
    pub execution_time: Option<i64>,
    // the checksum the history row holds
    pub applied_checksum: Option<Vec<u8>>,
}

/// The status of every version, with the history row's columns for the versions that have one.
#[must_use]
pub fn details(local: &[Migration], applied: &[AppliedRecord]) -> Vec<MigrationDetail> {
    migration_info(local, &plain(applied))
        .into_iter()
        .map(|info| {
            let record = applied.iter().find(|r| r.migration.version == info.version);
            MigrationDetail {
                installed_on_micros: record.map(|r| r.installed_on_micros),
                execution_time: record.map(|r| r.execution_time),
                applied_checksum: record.map(|r| r.migration.checksum.clone()),
                info,
            }
        })
        .collect()
}

#[must_use]
pub fn plain(records: &[AppliedRecord]) -> Vec<AppliedMigration> {
    records.iter().map(|r| r.migration.clone()).collect()
}

/// The statuses a plan's steps would have afterwards, for a dry run.
#[must_use]
pub fn planned(steps: &[Migration], status: MigrationStatus) -> Vec<MigrationDetail> {
    steps
        .iter()
        .map(|step| MigrationDetail {
            info: MigrationInfo {
                version: step.version,
                description: Some(step.description.clone()),
                status,
            },
            installed_on_micros: None,
            execution_time: None,
            applied_checksum: None,
        })
        .collect()
}

/// The message of a migration that failed; `source` is the backend's own error.
#[must_use]
pub fn failed_message(version: i64, ended_transaction: bool, source: &dyn Display) -> String {
    if ended_transaction {
        format!(
            "migration {version} failed: the file ended rowx's transaction (mark it \
             `-- no-transaction`); whatever it committed stays committed but is not recorded"
        )
    } else {
        format!("migration {version} failed: {source}")
    }
}

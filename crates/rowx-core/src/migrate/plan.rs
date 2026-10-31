use std::collections::{BTreeMap, BTreeSet};

use thiserror::Error;

use super::migration::{Migration, MigrationKind};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AppliedMigration {
    pub version: i64,
    pub checksum: Vec<u8>,
    pub success: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct PlanOptions {
    pub target: Option<i64>,
    pub ignore_missing: bool,
    pub allow_out_of_order: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum PlanError {
    #[error(
        "migration {version} is dirty: it did not finish; fix the database and its history row"
    )]
    Dirty { version: i64 },
    #[error("migration {version} is applied but missing from the migration directory")]
    Missing { version: i64 },
    #[error("migration {version} was modified after it was applied")]
    Checksum { version: i64 },
    #[error("migration {pending} is older than the latest applied migration {latest}")]
    Order { pending: i64, latest: i64 },
    #[error("target migration {target} does not exist")]
    TargetNotFound { target: i64 },
    #[error("migration {version} has no .down.sql file to revert it")]
    MissingDown { version: i64 },
}

impl PlanError {
    #[must_use]
    pub const fn version(&self) -> i64 {
        match self {
            Self::Dirty { version }
            | Self::Missing { version }
            | Self::Checksum { version }
            | Self::MissingDown { version } => *version,
            Self::Order { pending, .. } => *pending,
            Self::TargetNotFound { target } => *target,
        }
    }
}

// The migrations to carry out, in the order to carry them out. `run` and `skip` plans hold
// the forward (simple or up) migrations oldest first, an `undo` plan holds the down
// migrations newest first.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct RunPlan {
    pub steps: Vec<Migration>,
}

impl RunPlan {
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.steps.is_empty()
    }
}

// Dirty, then missing, then checksum; each reports its lowest version. Nothing runs before
// all three pass.
fn validate(
    forward: &BTreeMap<i64, &Migration>,
    applied: &[AppliedMigration],
    ignore_missing: bool,
) -> Result<(), PlanError> {
    let by_version: BTreeMap<i64, &AppliedMigration> =
        applied.iter().map(|row| (row.version, row)).collect();

    if let Some(version) = by_version
        .values()
        .find(|row| !row.success)
        .map(|row| row.version)
    {
        return Err(PlanError::Dirty { version });
    }
    if !ignore_missing && let Some(&version) = by_version.keys().find(|v| !forward.contains_key(v))
    {
        return Err(PlanError::Missing { version });
    }
    if let Some(&version) = by_version.iter().find_map(|(version, row)| {
        forward
            .get(version)
            .is_some_and(|m| m.checksum[..] != row.checksum[..])
            .then_some(version)
    }) {
        return Err(PlanError::Checksum { version });
    }
    Ok(())
}

fn forward_migrations(local: &[Migration]) -> BTreeMap<i64, &Migration> {
    local
        .iter()
        .filter(|m| !m.kind.is_down())
        .map(|m| (m.version, m))
        .collect()
}

///
/// # Errors
///
/// Returns the first failed validation (dirty, missing, checksum), an unknown or too old
/// target, or an out-of-order pending migration that is not allowed.
pub fn plan_run(
    local: &[Migration],
    applied: &[AppliedMigration],
    opts: PlanOptions,
) -> Result<RunPlan, PlanError> {
    let forward = forward_migrations(local);
    validate(&forward, applied, opts.ignore_missing)?;

    let latest = applied.iter().map(|row| row.version).max();
    if let Some(target) = opts.target {
        if !forward.contains_key(&target) {
            return Err(PlanError::TargetNotFound { target });
        }
        if let Some(latest) = latest
            && target < latest
        {
            return Err(PlanError::Order {
                pending: target,
                latest,
            });
        }
    }

    let done: BTreeSet<i64> = applied.iter().map(|row| row.version).collect();
    let steps: Vec<Migration> = forward
        .iter()
        .filter(|(version, _)| !done.contains(version))
        .filter(|(version, _)| opts.target.is_none_or(|target| **version <= target))
        .map(|(_, migration)| (*migration).clone())
        .collect();

    if !opts.allow_out_of_order
        && let Some(latest) = latest
        && let Some(stale) = steps.iter().find(|m| m.version < latest)
    {
        return Err(PlanError::Order {
            pending: stale.version,
            latest,
        });
    }
    Ok(RunPlan { steps })
}

// Skipping records the same migrations that `run` would apply, so the rules are the same
///
/// # Errors
///
/// The same as [`plan_run`].
pub fn plan_skip(
    local: &[Migration],
    applied: &[AppliedMigration],
    opts: PlanOptions,
) -> Result<RunPlan, PlanError> {
    plan_run(local, applied, opts)
}

// Reverts every applied version above `target` (`0` reverts all of them), newest first. A
// version in range without a `.down.sql` file fails the whole plan.
///
/// # Errors
///
/// Returns a failed validation, or [`PlanError::MissingDown`] for the newest version in range
/// that has no `.down.sql` file.
pub fn plan_undo(
    local: &[Migration],
    applied: &[AppliedMigration],
    target: i64,
    ignore_missing: bool,
) -> Result<RunPlan, PlanError> {
    let forward = forward_migrations(local);
    validate(&forward, applied, ignore_missing)?;

    let down: BTreeMap<i64, &Migration> = local
        .iter()
        .filter(|m| m.kind == MigrationKind::Down)
        .map(|m| (m.version, m))
        .collect();
    let in_range: BTreeSet<i64> = applied
        .iter()
        .map(|row| row.version)
        .filter(|version| *version > target)
        .collect();

    let steps = in_range
        .into_iter()
        .rev()
        .map(|version| {
            down.get(&version)
                .map(|migration| (*migration).clone())
                .ok_or(PlanError::MissingDown { version })
        })
        .collect::<Result<_, _>>()?;
    Ok(RunPlan { steps })
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use super::*;
    use crate::migrate::checksum;

    fn local(version: i64, kind: MigrationKind) -> Migration {
        let sql = format!("{version} {kind:?}");
        Migration {
            version,
            description: format!("m{version}"),
            kind,
            checksum: checksum(sql.as_bytes()),
            sql,
            no_transaction: false,
            path: PathBuf::new(),
        }
    }

    fn simple(versions: &[i64]) -> Vec<Migration> {
        versions
            .iter()
            .map(|v| local(*v, MigrationKind::Simple))
            .collect()
    }

    fn reversible(versions: &[i64]) -> Vec<Migration> {
        versions
            .iter()
            .flat_map(|v| [local(*v, MigrationKind::Up), local(*v, MigrationKind::Down)])
            .collect()
    }

    // A good row for the simple migration of `version`
    fn row(version: i64) -> AppliedMigration {
        let sql = format!("{version} {:?}", MigrationKind::Simple);
        AppliedMigration {
            version,
            checksum: checksum(sql.as_bytes()).to_vec(),
            success: true,
        }
    }

    fn up_row(version: i64) -> AppliedMigration {
        let sql = format!("{version} {:?}", MigrationKind::Up);
        AppliedMigration {
            version,
            checksum: checksum(sql.as_bytes()).to_vec(),
            success: true,
        }
    }

    fn rows(versions: &[i64]) -> Vec<AppliedMigration> {
        versions.iter().map(|v| row(*v)).collect()
    }

    fn versions(plan: &Result<RunPlan, PlanError>) -> Vec<i64> {
        plan.as_ref()
            .map(|p| p.steps.iter().map(|m| m.version).collect())
            .unwrap_or_default()
    }

    fn opts() -> PlanOptions {
        PlanOptions::default()
    }

    #[test]
    fn run_applies_every_pending_migration_in_version_order() {
        let migrations = simple(&[30, 10, 20]);
        let mut sorted = migrations;
        sorted.sort();
        assert_eq!(versions(&plan_run(&sorted, &[], opts())), [10, 20, 30]);
        assert_eq!(versions(&plan_run(&sorted, &rows(&[10]), opts())), [20, 30]);
        assert!(plan_run(&sorted, &rows(&[10, 20, 30]), opts()).is_ok_and(|p| p.is_empty()));
    }

    #[test]
    fn run_uses_the_up_file_of_a_reversible_migration_and_never_the_down_file() {
        let plan = plan_run(&reversible(&[1, 2]), &[], opts());
        assert_eq!(versions(&plan), [1, 2]);
        assert!(plan.is_ok_and(|p| p.steps.iter().all(|m| m.kind == MigrationKind::Up)));
    }

    #[test]
    fn a_dirty_row_is_an_error() {
        let mut applied = rows(&[1, 2]);
        applied[1].success = false;
        assert_eq!(
            plan_run(&simple(&[1, 2, 3]), &applied, opts()),
            Err(PlanError::Dirty { version: 2 })
        );
    }

    #[test]
    fn an_applied_version_without_a_local_file_is_missing() {
        assert_eq!(
            plan_run(&simple(&[1]), &rows(&[1, 7]), opts()),
            Err(PlanError::Missing { version: 7 })
        );
    }

    #[test]
    fn a_down_file_alone_does_not_count_as_a_local_migration() {
        let only_down = [local(4, MigrationKind::Down)];
        assert_eq!(
            plan_run(&only_down, &rows(&[4]), opts()),
            Err(PlanError::Missing { version: 4 })
        );
    }

    #[test]
    fn ignore_missing_allows_applied_versions_without_files() {
        let options = PlanOptions {
            ignore_missing: true,
            ..opts()
        };
        assert_eq!(
            versions(&plan_run(
                &simple(&[1, 8]),
                &rows(&[1, 7]),
                PlanOptions {
                    allow_out_of_order: true,
                    ..options
                }
            )),
            [8]
        );
    }

    #[test]
    fn a_modified_checksum_is_an_error() {
        let mut applied = rows(&[1, 2]);
        applied[1].checksum[0] ^= 0xff;
        assert_eq!(
            plan_run(&simple(&[1, 2]), &applied, opts()),
            Err(PlanError::Checksum { version: 2 })
        );
    }

    #[test]
    fn a_reversible_migration_compares_the_up_checksum() {
        assert!(plan_run(&reversible(&[1]), &[up_row(1)], opts()).is_ok());
        // a row that holds the checksum of the down file is modified
        let down_sql = format!("1 {:?}", MigrationKind::Down);
        let applied = [AppliedMigration {
            version: 1,
            checksum: checksum(down_sql.as_bytes()).to_vec(),
            success: true,
        }];
        assert_eq!(
            plan_run(&reversible(&[1]), &applied, opts()),
            Err(PlanError::Checksum { version: 1 })
        );
    }

    #[test]
    fn validation_runs_dirty_then_missing_then_checksum() {
        let migrations = simple(&[1, 2]);

        // all three problems at once: dirty wins
        let mut applied = rows(&[1, 2, 9]);
        applied[0].checksum[0] ^= 0xff;
        applied[1].success = false;
        assert_eq!(
            plan_run(&migrations, &applied, opts()),
            Err(PlanError::Dirty { version: 2 })
        );

        // missing and modified: missing wins
        let mut applied = rows(&[1, 9]);
        applied[0].checksum[0] ^= 0xff;
        assert_eq!(
            plan_run(&migrations, &applied, opts()),
            Err(PlanError::Missing { version: 9 })
        );

        // with ignore_missing the checksum is reached
        assert_eq!(
            plan_run(
                &migrations,
                &applied,
                PlanOptions {
                    ignore_missing: true,
                    ..opts()
                }
            ),
            Err(PlanError::Checksum { version: 1 })
        );
    }

    #[test]
    fn the_lowest_offending_version_is_reported() {
        let mut applied = rows(&[1, 2, 3]);
        applied[2].success = false;
        applied[1].success = false;
        assert_eq!(
            plan_run(&simple(&[1, 2, 3]), &applied, opts()),
            Err(PlanError::Dirty { version: 2 })
        );
        assert_eq!(
            plan_run(&simple(&[1]), &rows(&[1, 5, 4]), opts()),
            Err(PlanError::Missing { version: 4 })
        );
    }

    #[test]
    fn validation_errors_come_before_target_errors() {
        let mut applied = rows(&[1]);
        applied[0].success = false;
        let options = PlanOptions {
            target: Some(99),
            ..opts()
        };
        assert_eq!(
            plan_run(&simple(&[1]), &applied, options),
            Err(PlanError::Dirty { version: 1 })
        );
    }

    #[test]
    fn an_older_pending_migration_is_out_of_order_by_default() {
        assert_eq!(
            plan_run(&simple(&[1, 2, 3]), &rows(&[1, 3]), opts()),
            Err(PlanError::Order {
                pending: 2,
                latest: 3
            })
        );
    }

    #[test]
    fn out_of_order_can_be_allowed_and_runs_in_version_order() {
        let options = PlanOptions {
            allow_out_of_order: true,
            ..opts()
        };
        assert_eq!(
            versions(&plan_run(
                &simple(&[1, 2, 3, 4, 5]),
                &rows(&[1, 4]),
                options
            )),
            [2, 3, 5]
        );
    }

    #[test]
    fn the_out_of_order_check_covers_versions_missing_locally() {
        let options = PlanOptions {
            ignore_missing: true,
            ..opts()
        };
        assert_eq!(
            plan_run(&simple(&[1, 2]), &rows(&[1, 5]), options),
            Err(PlanError::Order {
                pending: 2,
                latest: 5
            })
        );
    }

    #[test]
    fn newer_pending_migrations_are_not_out_of_order() {
        assert_eq!(
            versions(&plan_run(&simple(&[1, 2, 3]), &rows(&[1, 2]), opts())),
            [3]
        );
    }

    #[test]
    fn run_stops_at_the_target_inclusive() {
        let migrations = simple(&[1, 2, 3, 4]);
        let options = PlanOptions {
            target: Some(3),
            ..opts()
        };
        assert_eq!(versions(&plan_run(&migrations, &[], options)), [1, 2, 3]);
        assert_eq!(
            versions(&plan_run(&migrations, &rows(&[1]), options)),
            [2, 3]
        );
    }

    #[test]
    fn run_to_the_latest_applied_version_is_a_no_op() {
        let options = PlanOptions {
            target: Some(2),
            ..opts()
        };
        assert!(plan_run(&simple(&[1, 2, 3]), &rows(&[1, 2]), options).is_ok_and(|p| p.is_empty()));
    }

    #[test]
    fn a_run_target_must_exist_locally() {
        let options = PlanOptions {
            target: Some(5),
            ..opts()
        };
        assert_eq!(
            plan_run(&simple(&[1, 2]), &[], options),
            Err(PlanError::TargetNotFound { target: 5 })
        );
        let zero = PlanOptions {
            target: Some(0),
            ..opts()
        };
        assert_eq!(
            plan_run(&simple(&[1]), &[], zero),
            Err(PlanError::TargetNotFound { target: 0 })
        );
    }

    #[test]
    fn a_down_only_version_is_not_a_run_target() {
        let options = PlanOptions {
            target: Some(2),
            ..opts()
        };
        assert_eq!(
            plan_run(&[local(2, MigrationKind::Down)], &[], options),
            Err(PlanError::TargetNotFound { target: 2 })
        );
    }

    #[test]
    fn a_run_target_below_the_latest_applied_is_an_order_error_even_when_out_of_order_is_allowed() {
        let options = PlanOptions {
            target: Some(1),
            allow_out_of_order: true,
            ..opts()
        };
        assert_eq!(
            plan_run(&simple(&[1, 2, 3]), &rows(&[2, 3]), options),
            Err(PlanError::Order {
                pending: 1,
                latest: 3
            })
        );
        let applied_target = PlanOptions {
            target: Some(2),
            ..opts()
        };
        assert_eq!(
            plan_run(&simple(&[1, 2, 3]), &rows(&[1, 2, 3]), applied_target),
            Err(PlanError::Order {
                pending: 2,
                latest: 3
            })
        );
    }

    #[test]
    fn target_and_out_of_order_combine() {
        let options = PlanOptions {
            target: Some(5),
            allow_out_of_order: true,
            ..opts()
        };
        assert_eq!(
            versions(&plan_run(
                &simple(&[1, 2, 3, 4, 5, 6]),
                &rows(&[1, 4]),
                options
            )),
            [2, 3, 5]
        );
    }

    #[test]
    fn skip_follows_the_run_rules() {
        let migrations = simple(&[1, 2, 3]);
        assert_eq!(versions(&plan_skip(&migrations, &[], opts())), [1, 2, 3]);
        let options = PlanOptions {
            target: Some(2),
            ..opts()
        };
        assert_eq!(versions(&plan_skip(&migrations, &[], options)), [1, 2]);
        assert_eq!(
            plan_skip(&migrations, &rows(&[1, 3]), opts()),
            Err(PlanError::Order {
                pending: 2,
                latest: 3
            })
        );
        let mut dirty = rows(&[1]);
        dirty[0].success = false;
        assert_eq!(
            plan_skip(&migrations, &dirty, opts()),
            Err(PlanError::Dirty { version: 1 })
        );
    }

    fn undo_versions(plan: &Result<RunPlan, PlanError>) -> Vec<(i64, MigrationKind)> {
        plan.as_ref()
            .map(|p| p.steps.iter().map(|m| (m.version, m.kind)).collect())
            .unwrap_or_default()
    }

    fn up_rows(versions: &[i64]) -> Vec<AppliedMigration> {
        versions.iter().map(|v| up_row(*v)).collect()
    }

    #[test]
    fn undo_reverts_newest_first_above_the_target() {
        let migrations = reversible(&[1, 2, 3]);
        let applied = up_rows(&[1, 2, 3]);
        assert_eq!(
            undo_versions(&plan_undo(&migrations, &applied, 1, false)),
            [(3, MigrationKind::Down), (2, MigrationKind::Down)]
        );
    }

    #[test]
    fn undo_to_zero_reverts_everything() {
        let migrations = reversible(&[1, 2]);
        let applied = up_rows(&[1, 2]);
        assert_eq!(
            undo_versions(&plan_undo(&migrations, &applied, 0, false)),
            [(2, MigrationKind::Down), (1, MigrationKind::Down)]
        );
        assert_eq!(
            undo_versions(&plan_undo(&migrations, &applied, -5, false)),
            [(2, MigrationKind::Down), (1, MigrationKind::Down)]
        );
    }

    #[test]
    fn undo_to_the_latest_or_beyond_is_a_no_op() {
        let migrations = reversible(&[1, 2]);
        let applied = up_rows(&[1, 2]);
        assert!(plan_undo(&migrations, &applied, 2, false).is_ok_and(|p| p.is_empty()));
        assert!(plan_undo(&migrations, &applied, 50, false).is_ok_and(|p| p.is_empty()));
        assert!(plan_undo(&migrations, &[], 0, false).is_ok_and(|p| p.is_empty()));
    }

    #[test]
    fn undo_only_considers_applied_versions() {
        let migrations = reversible(&[1, 2, 3]);
        assert_eq!(
            undo_versions(&plan_undo(&migrations, &up_rows(&[1, 2]), 0, false)),
            [(2, MigrationKind::Down), (1, MigrationKind::Down)]
        );
    }

    #[test]
    fn undo_refuses_before_any_step_when_a_down_file_is_missing() {
        // 1 is reversible, 2 is simple, 3 is reversible
        let mut migrations = reversible(&[1, 3]);
        migrations.push(local(2, MigrationKind::Simple));
        migrations.sort();
        let applied = vec![up_row(1), row(2), up_row(3)];
        assert_eq!(
            plan_undo(&migrations, &applied, 0, false),
            Err(PlanError::MissingDown { version: 2 })
        );
        // the version below the target is not in range, so the plan is fine
        assert_eq!(
            undo_versions(&plan_undo(&migrations, &applied, 2, false)),
            [(3, MigrationKind::Down)]
        );
    }

    #[test]
    fn undo_cannot_revert_a_version_it_has_no_files_for() {
        let migrations = reversible(&[1]);
        let applied = vec![up_row(1), row(9)];
        assert_eq!(
            plan_undo(&migrations, &applied, 0, false),
            Err(PlanError::Missing { version: 9 })
        );
        assert_eq!(
            plan_undo(&migrations, &applied, 0, true),
            Err(PlanError::MissingDown { version: 9 })
        );
        assert_eq!(
            undo_versions(&plan_undo(&migrations, &applied, 9, true)),
            []
        );
    }

    #[test]
    fn undo_validates_dirty_then_missing_then_checksum() {
        let migrations = reversible(&[1, 2]);

        let mut applied = up_rows(&[1, 2]);
        applied[1].success = false;
        assert_eq!(
            plan_undo(&migrations, &applied, 0, false),
            Err(PlanError::Dirty { version: 2 })
        );

        let mut applied = up_rows(&[1, 7]);
        applied[0].checksum[0] ^= 0xff;
        assert_eq!(
            plan_undo(&migrations, &applied, 0, false),
            Err(PlanError::Missing { version: 7 })
        );

        let mut applied = up_rows(&[1]);
        applied[0].checksum[0] ^= 0xff;
        assert_eq!(
            plan_undo(&migrations, &applied, 0, false),
            Err(PlanError::Checksum { version: 1 })
        );
    }

    #[test]
    fn errors_name_their_version_and_say_what_is_wrong() {
        let order = PlanError::Order {
            pending: 2,
            latest: 3,
        };
        assert_eq!(order.version(), 2);
        let message = order.to_string();
        assert!(message.contains('2') && message.contains('3'));
        assert_eq!(PlanError::Dirty { version: 4 }.version(), 4);
        assert_eq!(PlanError::Missing { version: 5 }.version(), 5);
        assert_eq!(PlanError::Checksum { version: 6 }.version(), 6);
        assert_eq!(PlanError::MissingDown { version: 7 }.version(), 7);
        assert_eq!(PlanError::TargetNotFound { target: 8 }.version(), 8);
    }
}

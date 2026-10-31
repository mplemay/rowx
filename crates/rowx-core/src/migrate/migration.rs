use std::cmp::Ordering;
use std::path::PathBuf;

use super::checksum::CHECKSUM_LEN;

// The derive order is the sort order, as in sqlx: simple, then up, then down
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum MigrationKind {
    Simple,
    Up,
    Down,
}

impl MigrationKind {
    #[must_use]
    pub const fn suffix(self) -> &'static str {
        match self {
            Self::Simple => ".sql",
            Self::Up => ".up.sql",
            Self::Down => ".down.sql",
        }
    }

    // Splits a file name that ends in `.sql` into the stem and the kind
    #[must_use]
    pub fn from_file_name(name: &str) -> Option<(&str, Self)> {
        [Self::Up, Self::Down, Self::Simple]
            .into_iter()
            .find_map(|kind| name.strip_suffix(kind.suffix()).map(|stem| (stem, kind)))
    }

    #[must_use]
    pub const fn is_down(self) -> bool {
        matches!(self, Self::Down)
    }
}

#[derive(Debug, Clone)]
pub struct Migration {
    pub version: i64,
    pub description: String,
    pub kind: MigrationKind,
    pub sql: String,
    pub checksum: [u8; CHECKSUM_LEN],
    pub no_transaction: bool,
    pub path: PathBuf,
}

// Identity is (version, kind), as in sqlx
impl PartialEq for Migration {
    fn eq(&self, other: &Self) -> bool {
        self.version == other.version && self.kind == other.kind
    }
}

impl Eq for Migration {}

impl PartialOrd for Migration {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for Migration {
    fn cmp(&self, other: &Self) -> Ordering {
        self.version
            .cmp(&other.version)
            .then_with(|| self.kind.cmp(&other.kind))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn migration(version: i64, kind: MigrationKind) -> Migration {
        Migration {
            version,
            description: String::new(),
            kind,
            sql: String::new(),
            checksum: [0; CHECKSUM_LEN],
            no_transaction: false,
            path: PathBuf::new(),
        }
    }

    #[test]
    fn kinds_are_read_from_the_suffix() {
        assert_eq!(
            MigrationKind::from_file_name("1_a.sql"),
            Some(("1_a", MigrationKind::Simple))
        );
        assert_eq!(
            MigrationKind::from_file_name("1_a.up.sql"),
            Some(("1_a", MigrationKind::Up))
        );
        assert_eq!(
            MigrationKind::from_file_name("1_a.down.sql"),
            Some(("1_a", MigrationKind::Down))
        );
        assert_eq!(MigrationKind::from_file_name("1_a.txt"), None);
    }

    #[test]
    fn migrations_order_by_version_then_kind() {
        let mut all = [
            migration(2, MigrationKind::Simple),
            migration(1, MigrationKind::Down),
            migration(1, MigrationKind::Up),
            migration(1, MigrationKind::Simple),
        ];
        all.sort();
        let order: Vec<_> = all.iter().map(|m| (m.version, m.kind)).collect();
        assert_eq!(
            order,
            [
                (1, MigrationKind::Simple),
                (1, MigrationKind::Up),
                (1, MigrationKind::Down),
                (2, MigrationKind::Simple),
            ]
        );
    }

    #[test]
    fn equality_ignores_everything_but_version_and_kind() {
        let mut other = migration(1, MigrationKind::Up);
        other.sql = "select 1".to_owned();
        assert_eq!(migration(1, MigrationKind::Up), other);
        assert_ne!(
            migration(1, MigrationKind::Up),
            migration(1, MigrationKind::Down)
        );
    }
}

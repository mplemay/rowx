use std::io;
use std::path::PathBuf;

use crate::snapshot::SnapshotError;

#[derive(Debug, thiserror::Error)]
pub enum HostError {
    #[error("engine: {0}")]
    Engine(String),
    #[error("io: {0}")]
    Io(#[from] io::Error),
    #[error("the engine thread is gone")]
    EngineGone,
    #[error("{}", locked_message(.directory, *.pid))]
    Locked {
        directory: PathBuf,
        pid: Option<u32>,
    },
    #[error("{0}")]
    Snapshot(#[from] SnapshotError),
    #[error("{0}")]
    InvalidArgument(String),
    #[error("the instance belongs to another process (it was forked)")]
    ForkedProcess,
    #[error("the instance is closed")]
    Closed,
    #[error("the embedded engine is unusable ({0}); close and restart the instance")]
    Unusable(String),
}

impl From<oliphaunt_wasix::Error> for HostError {
    fn from(error: oliphaunt_wasix::Error) -> Self {
        Self::Engine(format!("{error:#}"))
    }
}

fn locked_message(directory: &std::path::Path, pid: Option<u32>) -> String {
    let directory = directory.display();
    pid.map_or_else(
        || format!("database directory {directory} is in use by another instance"),
        |pid| format!("database directory {directory} is in use by pid {pid}"),
    )
}

pub type Result<T> = std::result::Result<T, HostError>;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_lock_error_names_the_pid_when_it_is_known() {
        let known = HostError::Locked {
            directory: "/data/db".into(),
            pid: Some(4242),
        };
        assert_eq!(
            known.to_string(),
            "database directory /data/db is in use by pid 4242"
        );
        let unknown = HostError::Locked {
            directory: "/data/db".into(),
            pid: None,
        };
        assert_eq!(
            unknown.to_string(),
            "database directory /data/db is in use by another instance"
        );
    }
}

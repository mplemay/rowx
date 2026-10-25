use thiserror::Error;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Error)]
pub enum CoreError {
    #[error("connection is closed")]
    Closed,
    #[error("connection is broken")]
    Broken,
    #[error("another operation is already running on this connection")]
    Busy,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Error)]
#[error("the operation task stopped unexpectedly")]
pub struct TaskStopped;

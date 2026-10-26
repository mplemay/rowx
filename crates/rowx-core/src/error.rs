use thiserror::Error;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Error)]
pub enum CoreError {
    #[error("connection is closed")]
    Closed,
    #[error("connection is broken")]
    Broken,
    #[error("another operation is already running on this connection")]
    Busy,
    #[error("timed out waiting for a pooled connection")]
    PoolTimeout,
    #[error("the pool is closed")]
    PoolClosed,
    #[error("the connection was released to the pool; acquire another one")]
    StaleLease,
    #[error("the pool was used in a forked process; create the pool after fork")]
    Forked,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Error)]
#[error("the operation task stopped unexpectedly")]
pub struct TaskStopped;

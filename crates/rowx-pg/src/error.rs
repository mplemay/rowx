use thiserror::Error;
use tokio_postgres::error::ErrorPosition;

#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum Error {
    #[error("{0}")]
    Interface(String),
    #[error("{sqlstate}: {message}")]
    Db {
        // boxed strs keep the enum small enough for clippy::result_large_err
        sqlstate: Box<str>,
        message: Box<str>,
        detail: Option<Box<str>>,
        hint: Option<Box<str>>,
        position: Option<u32>,
        routine: Option<Box<str>>,
    },
    #[error("connection is closed")]
    Closed,
    #[error("connection is broken")]
    Broken,
    #[error("another operation is already running on this connection")]
    Busy,
    #[error("{0}")]
    RowShape(String),
    #[error("{0}")]
    Decode(String),
}

impl From<tokio_postgres::Error> for Error {
    fn from(err: tokio_postgres::Error) -> Self {
        if let Some(db) = err.as_db_error() {
            return Self::Db {
                sqlstate: db.code().code().into(),
                message: db.message().into(),
                detail: db.detail().map(Into::into),
                hint: db.hint().map(Into::into),
                position: db.position().map(|p| match p {
                    ErrorPosition::Original(n) => *n,
                    ErrorPosition::Internal { position, .. } => *position,
                }),
                routine: db.routine().map(Into::into),
            };
        }
        if err.is_closed() {
            return Self::Closed;
        }
        Self::Interface(err.to_string())
    }
}

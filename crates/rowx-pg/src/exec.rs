use bytes::BytesMut;
use futures_util::{TryStreamExt, pin_mut};
use tokio_postgres::types::{FromSql, IsNull, ToSql, Type, to_sql_checked};
use tokio_postgres::{Client, Row, Statement};

use crate::error::Error;

/// The undecoded bytes of one cell (`None` for NULL); accepts every Postgres type.
#[derive(Debug, Clone, Copy)]
pub struct Raw<'a>(pub Option<&'a [u8]>);

impl<'a> FromSql<'a> for Raw<'a> {
    fn from_sql(
        _ty: &Type,
        raw: &'a [u8],
    ) -> Result<Self, Box<dyn std::error::Error + Sync + Send>> {
        Ok(Self(Some(raw)))
    }

    fn from_sql_null(_ty: &Type) -> Result<Self, Box<dyn std::error::Error + Sync + Send>> {
        Ok(Self(None))
    }

    fn accepts(_ty: &Type) -> bool {
        true
    }
}

/// One encoded parameter, ready for Bind. The bytes are never printed, since
/// tokio-postgres logs parameters through `Debug`.
#[derive(Clone)]
pub struct Param {
    value: Option<Vec<u8>>,
    format: tokio_postgres::types::Format,
}

impl Param {
    #[must_use]
    pub const fn null() -> Self {
        Self {
            value: None,
            format: tokio_postgres::types::Format::Binary,
        }
    }

    #[must_use]
    pub const fn binary(value: Vec<u8>) -> Self {
        Self {
            value: Some(value),
            format: tokio_postgres::types::Format::Binary,
        }
    }

    #[must_use]
    pub const fn text(value: Vec<u8>) -> Self {
        Self {
            value: Some(value),
            format: tokio_postgres::types::Format::Text,
        }
    }

    #[must_use]
    pub const fn is_text(&self) -> bool {
        matches!(self.format, tokio_postgres::types::Format::Text)
    }

    #[must_use]
    pub fn value(&self) -> Option<&[u8]> {
        self.value.as_deref()
    }
}

impl std::fmt::Debug for Param {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Param(<redacted>)")
    }
}

impl ToSql for Param {
    fn to_sql(
        &self,
        _ty: &Type,
        out: &mut BytesMut,
    ) -> Result<IsNull, Box<dyn std::error::Error + Sync + Send>> {
        let Some(bytes) = &self.value else {
            return Ok(IsNull::Yes);
        };
        out.extend_from_slice(bytes);
        Ok(IsNull::No)
    }

    fn accepts(_ty: &Type) -> bool {
        true
    }

    fn encode_format(&self, _ty: &Type) -> tokio_postgres::types::Format {
        self.format
    }

    to_sql_checked!();
}

/// Encodes the caller's parameters against the types the server reported for the statement.
pub trait Encode {
    /// # Errors
    /// `Interface` for a count mismatch, `Encode` when a value does not fit its parameter type.
    fn encode(self, types: &[Type]) -> Result<Vec<Param>, Error>;
}

impl<F: FnOnce(&[Type]) -> Result<Vec<Param>, Error>> Encode for F {
    fn encode(self, types: &[Type]) -> Result<Vec<Param>, Error> {
        self(types)
    }
}

async fn bind(
    client: &Client,
    sql: &str,
    encode: impl Encode,
) -> Result<(Statement, Vec<Param>), Error> {
    let stmt = client.prepare(sql).await?;
    let params = encode.encode(stmt.params())?;
    Ok((stmt, params))
}

/// What the caller expects of the result, checked before and while rows stream in.
#[derive(Debug, Clone, Copy, Default)]
pub struct Shape<'a> {
    /// `true` marks a nullable column, so `false` rejects NULL there.
    pub mask: Option<&'a [bool]>,
    pub scalar: bool,
    pub limit_one: bool,
    pub arity: Option<usize>,
}

/// # Errors
/// `RowShape` for an arity, scalar or NULL-mask violation, `Db`/`Interface` for driver failures.
pub async fn fetch(
    client: &Client,
    sql: &str,
    shape: &Shape<'_>,
    encode: impl Encode,
) -> Result<Vec<Row>, Error> {
    let (stmt, params) = bind(client, sql, encode).await?;
    let columns = stmt.columns().len();
    // checked before executing so a statement with no rows still reports a wrong shape
    if let Some(arity) = shape.arity
        && arity != columns
    {
        return Err(Error::RowShape(format!(
            "expected {arity} columns, the query returns {columns}"
        )));
    }
    if shape.scalar && columns != 1 {
        return Err(Error::RowShape(format!(
            "a scalar result needs exactly 1 column, the query returns {columns}"
        )));
    }
    if let Some(mask) = shape.mask
        && mask.len() != columns
    {
        return Err(Error::RowShape(format!(
            "the nullability mask covers {} columns, the query returns {columns}",
            mask.len()
        )));
    }
    let stream = client.query_raw(&stmt, &params).await?;
    pin_mut!(stream);
    let mut rows = Vec::new();
    while let Some(row) = stream.try_next().await? {
        if let Some(mask) = shape.mask {
            check_nulls(&row, mask)?;
        }
        rows.push(row);
        // dropping the stream discards the rest of the result; the connection task drains it
        if shape.limit_one {
            break;
        }
    }
    Ok(rows)
}

/// # Errors
/// `Db`/`Interface` for driver failures, `Encode` for a parameter that does not fit.
pub async fn execute(client: &Client, sql: &str, encode: impl Encode) -> Result<u64, Error> {
    let (stmt, params) = bind(client, sql, encode).await?;
    Ok(client.execute_raw(&stmt, &params).await?)
}

fn check_nulls(row: &Row, mask: &[bool]) -> Result<(), Error> {
    for (index, nullable) in mask.iter().enumerate() {
        if !nullable && row.try_get::<_, Raw>(index)?.0.is_none() {
            return Err(Error::RowShape(format!(
                "NULL in a non-nullable column at index {index}"
            )));
        }
    }
    Ok(())
}

use futures_util::{TryStreamExt, pin_mut};
use tokio_postgres::types::{FromSql, ToSql, Type};
use tokio_postgres::{Client, Row};

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
pub async fn fetch(client: &Client, sql: &str, shape: &Shape<'_>) -> Result<Vec<Row>, Error> {
    let stmt = client.prepare(sql).await?;
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
    let stream = client
        .query_raw(&stmt, std::iter::empty::<&(dyn ToSql + Sync)>())
        .await?;
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
/// `Db`/`Interface` for driver failures.
pub async fn execute(client: &Client, sql: &str) -> Result<u64, Error> {
    Ok(client.execute(sql, &[]).await?)
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

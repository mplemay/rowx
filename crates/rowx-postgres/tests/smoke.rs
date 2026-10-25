// pub, so the shared helpers are reachable from the crate root and need no visibility waiver
pub mod common;

use common::TestResult;

#[tokio::test]
async fn select_one_in_fresh_schema() -> TestResult {
    let Some(schema) = common::fresh_schema().await? else {
        return Ok(());
    };
    let client = schema.connect().await?;

    let row = client.query_one("SELECT 1::int4", &[]).await?;
    assert_eq!(row.get::<_, i32>(0), 1);

    let row = client.query_one("SELECT current_schema()", &[]).await?;
    assert_eq!(row.get::<_, String>(0), schema.name);

    let row = client.query_one("SELECT count(*) FROM users", &[]).await?;
    assert_eq!(row.get::<_, i64>(0), 3);

    schema.drop().await
}

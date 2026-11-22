pub mod cancel;
pub mod conn;
pub mod describe;
pub mod dsn;
pub mod error;
pub mod exec;
pub mod migrate;
pub mod pool;
mod timeout;
pub mod types;
pub mod value;

#[must_use]
pub fn sqlite_version() -> &'static str {
    rusqlite::version()
}

#[cfg(test)]
mod tests {
    use super::sqlite_version;

    #[test]
    fn bundled_sqlite_is_the_pinned_release() {
        assert_eq!(sqlite_version(), "3.51.3");
    }
}

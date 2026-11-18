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

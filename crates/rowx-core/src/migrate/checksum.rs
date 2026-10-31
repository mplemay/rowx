use sha2::{Digest, Sha384};

pub const CHECKSUM_LEN: usize = 48;

// SHA-384 over the raw bytes with no normalisation, so digests equal sqlx's byte for byte
#[must_use]
pub fn checksum(bytes: &[u8]) -> [u8; CHECKSUM_LEN] {
    Sha384::digest(bytes).into()
}

#[cfg(test)]
mod tests {
    use std::fmt::Write;

    use super::{CHECKSUM_LEN, checksum};

    fn hex(bytes: &[u8]) -> String {
        bytes.iter().fold(String::new(), |mut out, b| {
            let _ = write!(out, "{b:02x}");
            out
        })
    }

    #[test]
    fn matches_the_sha384_test_vectors() {
        assert_eq!(
            hex(&checksum(b"")),
            "38b060a751ac96384cd9327eb1b1e36a21fdb71114be07434c0cc7bf63f6e1da274edebfe76f65fbd51ad2f14898b95b"
        );
        assert_eq!(
            hex(&checksum(b"abc")),
            "cb00753f45a35e8bb5a03d699ac65007272c32ab0eded1631a8b605a43ff5bed8086072ba1e7cc2358baeca134c825a7"
        );
        assert_eq!(checksum(b"abc").len(), CHECKSUM_LEN);
    }

    // sqlx's own in-code copy of `examples/postgres/axum-social-with-tests/migrations/3_comment.sql`
    // (BOM, CRLF and tabs, from the checksum tests in `sqlx-core/src/migrate/source.rs`). The
    // digest was computed independently with Python's hashlib.
    #[test]
    fn matches_sqlx_for_its_comment_fixture() {
        let sql = "\
            \u{FEFF}create table comment (\r\n\
                \tcomment_id uuid primary key default gen_random_uuid(),\r\n\
                \tpost_id uuid not null references post(post_id),\r\n\
                \tuser_id uuid not null references \"user\"(user_id),\r\n\
                \tcontent text not null,\r\n\
                \tcreated_at timestamptz not null default now()\r\n\
            );\r\n\
            \r\n\
            create index on comment(post_id, created_at);\r\n\
        ";
        assert_eq!(
            hex(&checksum(sql.as_bytes())),
            "76e26521eb0bc3013e6df15ea4dc76e8fba2c511bec39ed411f6bb32c7aa5c3f190ebf1ea12461ccd4f8e3763c348306"
        );
    }

    // The same file as committed in sqlx (no BOM, LF, spaces), byte for byte
    #[test]
    fn matches_sqlx_for_its_comment_file_on_disk() {
        let sql = "\
            create table comment (\n    \
                comment_id uuid primary key default gen_random_uuid(),\n    \
                post_id uuid not null references post(post_id),\n    \
                user_id uuid not null references \"user\"(user_id),\n    \
                content text not null,\n    \
                created_at timestamptz not null default now()\n\
            );\n\
            \n\
            create index on comment(post_id, created_at);\n\
        ";
        assert_eq!(sql.len(), 315);
        assert_eq!(
            hex(&checksum(sql.as_bytes())),
            "1f3fdab5b6c7c72c271b827d1517919bceb4a78a14866b33bd77fb7a3b95161b119b4f3204147a2adaf17782aa4ec147"
        );
    }

    #[test]
    fn line_endings_are_not_normalised() {
        assert_ne!(checksum(b"select 1;\n"), checksum(b"select 1;\r\n"));
    }
}

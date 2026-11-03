use std::collections::BTreeSet;
use std::fmt::Write as _;
use std::sync::{Mutex, MutexGuard, PoisonError};

pub const MASK: &str = "***";

// every secret this process has parsed, so text that never saw the DSN (a wrapped cause's repr) is covered
static KNOWN: Mutex<BTreeSet<String>> = Mutex::new(BTreeSet::new());

fn known() -> MutexGuard<'static, BTreeSet<String>> {
    KNOWN.lock().unwrap_or_else(PoisonError::into_inner)
}

/// Remembers a secret value. Empty secrets are skipped.
pub fn register(secret: &str) {
    if !secret.is_empty() {
        known().insert(secret.to_owned());
    }
}

fn percent_encode(s: &str, lower: bool) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'.' | b'_' | b'~') {
            out.push(char::from(b));
        } else if lower {
            let _ = write!(out, "%{b:02x}");
        } else {
            let _ = write!(out, "%{b:02X}");
        }
    }
    out
}

// the quote- and backslash-escaped spellings of a key=value DSN
fn escaped(s: &str, whitespace: bool) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        if c == '\\' || c == '\'' || (whitespace && c.is_whitespace()) {
            out.push('\\');
        }
        out.push(c);
    }
    out
}

// a key=value DSN value without its quotes and backslashes
fn unescape(raw: &str) -> String {
    let inner = raw.strip_prefix('\'').unwrap_or(raw);
    let mut out = String::with_capacity(inner.len());
    let mut chars = inner.chars();
    while let Some(c) = chars.next() {
        match c {
            '\\' => out.extend(chars.next()),
            '\'' => {}
            _ => out.push(c),
        }
    }
    out
}

fn spellings(secret: &str, out: &mut BTreeSet<String>) {
    if secret.is_empty() {
        return;
    }
    out.insert(secret.to_owned());
    out.insert(percent_encode(secret, false));
    out.insert(percent_encode(secret, true));
    out.insert(escaped(secret, false));
    out.insert(escaped(secret, true));
}

// the raw value of each `*password` key and of the URL userinfo, with the extent of the value
fn secret_spans(text: &str) -> Vec<(usize, usize)> {
    let bytes = text.as_bytes();
    let lower = text.to_ascii_lowercase();
    let mut spans = Vec::new();
    let mut from = 0;
    while let Some(found) = lower[from..].find("password") {
        let mut i = from + found + "password".len();
        from = i;
        while bytes.get(i).is_some_and(u8::is_ascii_whitespace) {
            i += 1;
        }
        if bytes.get(i) != Some(&b'=') {
            continue;
        }
        i += 1;
        while bytes.get(i).is_some_and(u8::is_ascii_whitespace) {
            i += 1;
        }
        let start = i;
        if bytes.get(i) == Some(&b'\'') {
            i += 1;
            while let Some(&b) = bytes.get(i) {
                if b == b'\\' {
                    i += 2;
                } else if b == b'\'' {
                    i += 1;
                    break;
                } else {
                    i += 1;
                }
            }
            i = i.min(bytes.len());
        } else {
            while bytes
                .get(i)
                .is_some_and(|b| !b.is_ascii_whitespace() && *b != b'&')
            {
                i += 1;
            }
        }
        if i > start {
            spans.push((start, i));
            from = i;
        }
    }
    let mut at = 0;
    while let Some(found) = text[at..].find("://") {
        let auth = at + found + 3;
        let end = text[auth..]
            .find(|c: char| matches!(c, '/' | '?' | '#') || c.is_whitespace())
            .map_or(text.len(), |n| auth + n);
        at = end.max(auth);
        if let Some(userinfo_end) = text[auth..end].rfind('@').map(|n| auth + n)
            && let Some(colon) = text[auth..userinfo_end].find(':').map(|n| auth + n + 1)
            && userinfo_end > colon
        {
            spans.push((colon, userinfo_end));
        }
    }
    spans
}

// the raw secret text found in `text`, syntactically
fn raw_secrets(text: &str) -> Vec<String> {
    let mut out = Vec::new();
    for (start, end) in secret_spans(text) {
        if let Some(raw) = text.get(start..end) {
            out.push(raw.to_owned());
        }
    }
    out
}

/// Masks URL userinfo passwords and `*password=` values by syntax, so an unparseable DSN is covered.
#[must_use]
pub fn mask_syntax(text: &str) -> String {
    let mut spans = secret_spans(text);
    spans.sort_unstable();
    let mut out = String::with_capacity(text.len());
    let mut cursor = 0;
    for (start, end) in spans {
        if start < cursor {
            continue;
        }
        if let (Some(before), Some(_)) = (text.get(cursor..start), text.get(start..end)) {
            out.push_str(before);
            out.push_str(MASK);
            cursor = end;
        }
    }
    out.push_str(text.get(cursor..).unwrap_or_default());
    out
}

/// Redacts `text` by secret value (the registry, the secrets of `dsn`, and every raw spelling of them),
/// then masks the remaining userinfo and `password=` tokens by syntax.
#[must_use]
pub fn redact(text: &str, dsn: Option<&str>) -> String {
    let mut secrets = BTreeSet::new();
    for secret in known().iter() {
        spellings(secret, &mut secrets);
    }
    if let Some(dsn) = dsn {
        for raw in raw_secrets(dsn) {
            let plain = unescape(&raw);
            spellings(&raw, &mut secrets);
            spellings(&crate::dsn::percent_decode(&raw), &mut secrets);
            spellings(&plain, &mut secrets);
            spellings(&crate::dsn::percent_decode(&plain), &mut secrets);
        }
    }
    let mut ordered: Vec<&String> = secrets.iter().collect();
    ordered.sort_by_key(|s| std::cmp::Reverse(s.len()));
    let mut out = text.to_owned();
    for secret in ordered {
        out = out.replace(secret.as_str(), MASK);
    }
    mask_syntax(&out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn syntax_masks_userinfo_and_password_tokens() {
        assert_eq!(
            mask_syntax("postgres://u:p%40ss@h/db?sslpassword=x&a=b"),
            "postgres://u:***@h/db?sslpassword=***&a=b"
        );
        assert_eq!(
            mask_syntax("host=h password='a b\\'c' user=u"),
            "host=h password=*** user=u"
        );
        assert_eq!(mask_syntax("password='unterminated"), "password=***");
        assert_eq!(mask_syntax("postgres://u@h/db"), "postgres://u@h/db");
    }

    #[test]
    fn values_are_replaced_in_every_spelling() {
        let dsn = "postgres://u:p%40ss@h/db";
        for text in ["p%40ss", "p@ss"] {
            let out = redact(&format!("boom {text}"), Some(dsn));
            assert!(!out.contains("ss"), "{out}");
        }
        assert_eq!(
            redact("nothing here", Some("host=h password=")),
            "nothing here"
        );
    }

    #[test]
    fn registry_covers_text_without_a_dsn() {
        register("zq-registry-secret");
        assert!(!redact("cause: zq-registry-secret", None).contains("zq-registry-secret"));
        register("");
        assert!(redact("plain text", None).contains(' '));
    }
}

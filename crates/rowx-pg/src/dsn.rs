use tokio_postgres::Config;
use tokio_postgres::config::SslMode;

use crate::error::Error;

const SSL_KEYS: [&str; 5] = ["sslmode", "sslrootcert", "sslcert", "sslkey", "sslcrl"];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RequestedSsl {
    Unset,
    Disable,
}

#[derive(Debug)]
pub struct ParsedDsn {
    pub config: Config,
    pub ssl: RequestedSsl,
}

fn is_ssl_key(key: &str) -> bool {
    SSL_KEYS.contains(&key)
}

// error text never includes any part of the DSN, since it can carry the password
fn invalid(what: &str) -> Error {
    Error::Interface(format!("invalid connection string: {what}"))
}

/// # Errors
/// `Interface` for malformed DSNs and for `ssl*` settings that are not supported yet.
pub fn parse(dsn: &str) -> Result<ParsedDsn, Error> {
    let dsn = dsn.trim();
    let (stripped, ssl_pairs) =
        if dsn.starts_with("postgres://") || dsn.starts_with("postgresql://") {
            split_url(dsn)?
        } else {
            split_key_value(dsn)?
        };

    let mut ssl = RequestedSsl::Unset;
    for (key, value) in ssl_pairs {
        match (key.as_str(), value.as_str()) {
            ("sslmode", "disable") => ssl = RequestedSsl::Disable,
            ("sslmode", _) => {
                return Err(Error::Interface(
                    "sslmode other than 'disable' is not supported yet".to_owned(),
                ));
            }
            _ => {
                return Err(Error::Interface(format!("{key} is not supported yet")));
            }
        }
    }

    let mut config: Config = stripped
        .parse()
        .map_err(|_| invalid("could not be parsed"))?;
    if ssl == RequestedSsl::Disable {
        config.ssl_mode(SslMode::Disable);
    }
    Ok(ParsedDsn { config, ssl })
}

type Split = (String, Vec<(String, String)>);

fn split_url(dsn: &str) -> Result<Split, Error> {
    let (base, query) = match dsn.split_once('?') {
        Some((base, query)) => (base, query),
        None => (dsn, ""),
    };
    let mut kept = Vec::new();
    let mut ssl = Vec::new();
    for pair in query.split('&').filter(|p| !p.is_empty()) {
        let (key, value) = pair
            .split_once('=')
            .ok_or_else(|| invalid("malformed query"))?;
        if is_ssl_key(key) {
            ssl.push((key.to_owned(), percent_decode(value)));
        } else {
            kept.push(pair);
        }
    }
    let url = if kept.is_empty() {
        base.to_owned()
    } else {
        format!("{base}?{}", kept.join("&"))
    };
    Ok((url, ssl))
}

fn percent_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        let decoded = (bytes[i] == b'%')
            .then(|| bytes.get(i + 1..i + 3))
            .flatten()
            .and_then(|hex| std::str::from_utf8(hex).ok())
            .and_then(|hex| u8::from_str_radix(hex, 16).ok());
        if let Some(b) = decoded {
            out.push(b);
            i += 3;
        } else {
            out.push(bytes[i]);
            i += 1;
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn split_key_value(dsn: &str) -> Result<Split, Error> {
    let mut kept = Vec::new();
    let mut ssl = Vec::new();
    let mut chars = dsn.chars().peekable();
    loop {
        while chars.next_if(|c| c.is_whitespace()).is_some() {}
        if chars.peek().is_none() {
            break;
        }
        let mut key = String::new();
        while let Some(c) = chars.next_if(|c| *c != '=' && !c.is_whitespace()) {
            key.push(c);
        }
        while chars.next_if(|c| c.is_whitespace()).is_some() {}
        if chars.next_if_eq(&'=').is_none() || key.is_empty() {
            return Err(invalid("expected key=value"));
        }
        while chars.next_if(|c| c.is_whitespace()).is_some() {}
        let value = read_value(&mut chars)?;
        if is_ssl_key(&key) {
            ssl.push((key, value));
        } else {
            kept.push(format!("{key}={}", quote(&value)));
        }
    }
    Ok((kept.join(" "), ssl))
}

fn read_value(chars: &mut std::iter::Peekable<std::str::Chars<'_>>) -> Result<String, Error> {
    let mut value = String::new();
    if chars.next_if_eq(&'\'').is_some() {
        loop {
            match chars.next() {
                Some('\'') => return Ok(value),
                Some('\\') => {
                    value.push(chars.next().ok_or_else(|| invalid("trailing backslash"))?);
                }
                Some(c) => value.push(c),
                None => return Err(invalid("unterminated quote")),
            }
        }
    }
    while let Some(c) = chars.next_if(|c| !c.is_whitespace()) {
        if c == '\\' {
            value.push(chars.next().ok_or_else(|| invalid("trailing backslash"))?);
        } else {
            value.push(c);
        }
    }
    Ok(value)
}

fn quote(value: &str) -> String {
    let escaped = value.replace('\\', "\\\\").replace('\'', "\\'");
    format!("'{escaped}'")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn host(dsn: &str) -> Result<(Vec<String>, RequestedSsl), Error> {
        let parsed = parse(dsn)?;
        let hosts = parsed
            .config
            .get_hosts()
            .iter()
            .map(|h| format!("{h:?}"))
            .collect();
        Ok((hosts, parsed.ssl))
    }

    #[test]
    fn url_form() -> Result<(), Error> {
        let parsed = parse("postgres://u:p@localhost:5433/db")?;
        assert_eq!(parsed.config.get_user(), Some("u"));
        assert_eq!(parsed.config.get_dbname(), Some("db"));
        assert_eq!(parsed.config.get_ports(), &[5433]);
        assert_eq!(parsed.ssl, RequestedSsl::Unset);
        Ok(())
    }

    #[test]
    fn url_strips_ssl_keys_and_keeps_others() -> Result<(), Error> {
        let parsed = parse("postgresql://u@h/db?sslmode=disable&application_name=x")?;
        assert_eq!(parsed.ssl, RequestedSsl::Disable);
        assert_eq!(parsed.config.get_application_name(), Some("x"));
        assert_eq!(parsed.config.get_ssl_mode(), SslMode::Disable);
        Ok(())
    }

    #[test]
    fn key_value_form() -> Result<(), Error> {
        let parsed =
            parse("host=localhost port=5433 user=u password='a b\\'c' dbname=db sslmode=disable")?;
        assert_eq!(parsed.config.get_password(), Some(&b"a b'c"[..]));
        assert_eq!(parsed.config.get_ports(), &[5433]);
        assert_eq!(parsed.ssl, RequestedSsl::Disable);
        assert_eq!(host("host=h")?.0.len(), 1);
        Ok(())
    }

    #[test]
    fn unsupported_ssl_settings_error() {
        for dsn in [
            "postgres://u@h/db?sslmode=require",
            "postgres://u@h/db?sslrootcert=/x",
            "host=h sslmode=verify-full",
            "host=h sslcert=/x",
        ] {
            assert!(matches!(parse(dsn), Err(Error::Interface(_))), "{dsn}");
        }
    }

    #[test]
    fn parse_errors_never_echo_the_dsn() {
        for dsn in [
            "host=h password=hunter2 port=notaport",
            "password=hunter2 oops",
            "host=h password='hunter2",
        ] {
            assert!(
                parse(dsn).is_err_and(|err| !err.to_string().contains("hunter2")),
                "{dsn}"
            );
        }
    }
}

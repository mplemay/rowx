use std::fmt;
use std::time::Duration;

use tokio_postgres::Config;
use tokio_postgres::config::Host;

use crate::error::Error;
use crate::redact;
use crate::tls::{Mode, TlsSettings};

pub const DEFAULT_STATEMENT_CACHE_SIZE: usize = 256;

#[derive(Debug)]
pub struct ParsedDsn {
    pub config: Config,
    pub tls: TlsSettings,
    // the password and `*password` extras, which an error on the connect path must not echo
    secrets: Secrets,
}

// the count only, so a Debug print of the parsed options never shows a secret
struct Secrets(Vec<String>);

impl fmt::Debug for Secrets {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "<{} redacted>", self.0.len())
    }
}

impl ParsedDsn {
    /// Masks the secrets of these options in an `Interface` message; other errors pass through.
    #[must_use]
    pub fn redact_error(&self, err: Error) -> Error {
        redact_interface(err, &self.secrets.0)
    }
}

fn redact_interface(err: Error, secrets: &[String]) -> Error {
    match err {
        Error::Interface(message) => Error::Interface(redact::redact_secrets(&message, secrets)),
        other => other,
    }
}

// error text never includes any part of the DSN, since it can carry the password
fn invalid(what: &str) -> Error {
    Error::Interface(format!("invalid connection string: {what}"))
}

/// Everything a connection needs, in a typed form that `build` turns into a `tokio_postgres::Config`.
#[derive(Clone, PartialEq, Eq)]
pub struct ConnectOptions {
    pub host: Option<String>,
    pub port: Option<u16>,
    pub user: Option<String>,
    pub password: Option<String>,
    pub database: Option<String>,
    pub sslmode: Option<String>,
    pub sslrootcert: Option<String>,
    pub connect_timeout: Option<Duration>,
    pub statement_cache_size: usize,
    pub application_name: Option<String>,
    // sent in the startup `options` as `-c k=v`, so they survive `RESET ALL`
    pub server_settings: Vec<(String, String)>,
    // every other key the DSN carried (keepalives, target_session_attrs, several hosts, ...)
    pub extra: Vec<(String, String)>,
}

impl Default for ConnectOptions {
    fn default() -> Self {
        Self {
            host: None,
            port: None,
            user: None,
            password: None,
            database: None,
            sslmode: None,
            sslrootcert: None,
            connect_timeout: None,
            statement_cache_size: DEFAULT_STATEMENT_CACHE_SIZE,
            application_name: None,
            server_settings: Vec::new(),
            extra: Vec::new(),
        }
    }
}

// the password and the extra values (which may hold secrets) never reach a Debug print
impl fmt::Debug for ConnectOptions {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ConnectOptions")
            .field("host", &self.host)
            .field("port", &self.port)
            .field("user", &self.user)
            .field("password", &self.password.as_ref().map(|_| "<redacted>"))
            .field("database", &self.database)
            .field("sslmode", &self.sslmode)
            .field("sslrootcert", &self.sslrootcert)
            .field("connect_timeout", &self.connect_timeout)
            .field("statement_cache_size", &self.statement_cache_size)
            .field("application_name", &self.application_name)
            .field("server_settings", &self.server_settings)
            .field(
                "extra",
                &self.extra.iter().map(|(k, _)| k).collect::<Vec<_>>(),
            )
            .finish()
    }
}

fn upsert(pairs: &mut Vec<(String, String)>, key: &str, value: String) {
    match pairs.iter_mut().find(|(k, _)| k == key) {
        Some(slot) => slot.1 = value,
        None => pairs.push((key.to_owned(), value)),
    }
}

impl ConnectOptions {
    /// # Errors
    /// `Interface` for malformed DSNs, an unusable `sslmode`, and the client-certificate keys.
    pub fn from_dsn(dsn: &str) -> Result<Self, Error> {
        let dsn = dsn.trim();
        match Self::parse_text(dsn).and_then(|opts| opts.validate().map(|()| opts)) {
            Ok(opts) => Ok(opts),
            // no message may echo a secret, however the DSN spelled it
            Err(Error::Interface(message)) => {
                Err(Error::Interface(redact::redact(&message, Some(dsn))))
            }
            Err(other) => Err(other),
        }
    }

    fn parse_text(dsn: &str) -> Result<Self, Error> {
        let mut opts = Self::default();
        let pairs = if dsn.starts_with("postgres://") || dsn.starts_with("postgresql://") {
            let (base, pairs) = split_url(dsn)?;
            opts.apply_base(base)?;
            pairs
        } else {
            split_key_value(dsn)?
        };
        for (key, value) in pairs {
            opts.apply(&key, value)?;
        }
        Ok(opts)
    }

    fn secrets(&self) -> Vec<String> {
        let mut found: Vec<String> = self.password.iter().cloned().collect();
        found.extend(
            self.extra
                .iter()
                .filter(|(key, _)| key.contains("password"))
                .map(|(_, value)| value.clone()),
        );
        found
    }

    /// Masks the password and the `*password` extras of these options in an `Interface` message.
    #[must_use]
    pub fn redact_error(&self, err: Error) -> Error {
        redact_interface(err, &self.secrets())
    }

    /// # Errors
    /// `Interface` for a malformed `server_settings` or `extra` key, and for a NUL or other C0 control
    /// character in any value. The message names the key and never the value.
    pub fn validate(&self) -> Result<(), Error> {
        let typed = [
            ("host", &self.host),
            ("user", &self.user),
            ("password", &self.password),
            ("database", &self.database),
            ("sslmode", &self.sslmode),
            ("sslrootcert", &self.sslrootcert),
            ("application_name", &self.application_name),
        ];
        for (name, value) in typed {
            if let Some(value) = value {
                check_value(name, value)?;
            }
        }
        for (key, value) in &self.server_settings {
            if !is_guc_name(key) {
                return Err(invalid(&format!(
                    "malformed server setting name {}",
                    shown(key)
                )));
            }
            check_value(key, value)?;
        }
        for (key, value) in &self.extra {
            if key.is_empty() || !key.bytes().all(|b| b.is_ascii_lowercase() || b == b'_') {
                return Err(invalid(&format!("malformed key {}", shown(key))));
            }
            check_value(key, value)?;
        }
        Ok(())
    }

    // the URL authority and path, which tokio-postgres already knows how to read
    fn apply_base(&mut self, base: &str) -> Result<(), Error> {
        let config: Config = base.parse().map_err(|_| invalid("could not be parsed"))?;
        self.user = config.get_user().map(str::to_owned);
        self.password = config
            .get_password()
            .map(|p| String::from_utf8_lossy(p).into_owned());
        self.database = config.get_dbname().map(str::to_owned);
        let hosts: Vec<String> = config
            .get_hosts()
            .iter()
            .map(|host| match host {
                Host::Tcp(name) => name.clone(),
                #[cfg(unix)]
                Host::Unix(path) => path.to_string_lossy().into_owned(),
            })
            .collect();
        match hosts.as_slice() {
            [] => {}
            [one] => self.host = Some(one.clone()),
            _ => upsert(&mut self.extra, "host", hosts.join(",")),
        }
        match config.get_ports() {
            [] => {}
            [one] => self.port = Some(*one),
            many => {
                let ports: Vec<String> = many.iter().map(ToString::to_string).collect();
                upsert(&mut self.extra, "port", ports.join(","));
            }
        }
        Ok(())
    }

    fn apply(&mut self, key: &str, value: String) -> Result<(), Error> {
        match key {
            "host" if !value.contains(',') => {
                self.extra.retain(|(k, _)| k != "host");
                self.host = Some(value);
            }
            "host" => {
                self.host = None;
                upsert(&mut self.extra, "host", value);
            }
            "port" if !value.contains(',') => {
                self.extra.retain(|(k, _)| k != "port");
                self.port = Some(value.parse().map_err(|_| invalid("invalid port"))?);
            }
            "port" => {
                self.port = None;
                upsert(&mut self.extra, "port", value);
            }
            "user" => self.user = Some(value),
            "password" => self.password = Some(value),
            "dbname" => self.database = Some(value),
            "application_name" => self.application_name = Some(value),
            "connect_timeout" => {
                let secs: f64 = value
                    .parse()
                    .map_err(|_| invalid("invalid connect_timeout"))?;
                let timeout = Duration::try_from_secs_f64(secs)
                    .map_err(|_| invalid("invalid connect_timeout"))?;
                // libpq reads 0 as no timeout
                self.connect_timeout = (!timeout.is_zero()).then_some(timeout);
            }
            "options" => {
                check_value("options", &value)?;
                self.apply_options(&value)?;
            }
            "sslmode" => {
                Mode::parse(&value)?;
                self.sslmode = Some(value);
            }
            "sslrootcert" => self.sslrootcert = Some(value),
            // client certificates and CRLs are out of scope for v1
            "sslcert" | "sslkey" | "sslcrl" => {
                return Err(Error::Interface(format!("{key} is not supported in v1")));
            }
            _ => upsert(&mut self.extra, key, value),
        }
        Ok(())
    }

    // `-c k=v`, `-ck=v` and `--k=v` become server settings; anything else is kept verbatim in extra
    fn apply_options(&mut self, options: &str) -> Result<(), Error> {
        let mut leftover = Vec::new();
        let mut tokens = split_options(options).into_iter();
        while let Some(token) = tokens.next() {
            let setting = if token == "-c" {
                Some(tokens.next().ok_or_else(|| invalid("malformed options"))?)
            } else if let Some(rest) = token.strip_prefix("-c") {
                Some(rest.to_owned())
            } else {
                // like the server, `--search-path=x` is `search_path=x`
                token.strip_prefix("--").and_then(|rest| {
                    let (name, value) = rest.split_once('=')?;
                    Some(format!("{}={value}", name.replace('-', "_")))
                })
            };
            match setting {
                Some(setting) => {
                    let (key, value) = setting
                        .split_once('=')
                        .ok_or_else(|| invalid("malformed options"))?;
                    // `ParseLongOption` maps `-` to `_` in the name of a `-c` setting as well
                    upsert(
                        &mut self.server_settings,
                        &key.replace('-', "_"),
                        value.to_owned(),
                    );
                }
                None => leftover.push(escape_option(&token)),
            }
        }
        if !leftover.is_empty() {
            upsert(&mut self.extra, "options", leftover.join(" "));
        }
        Ok(())
    }

    /// # Errors
    /// `Interface` for an unusable `sslmode`, a malformed setting name, or a key the driver rejects.
    pub fn build(&self) -> Result<ParsedDsn, Error> {
        self.build_unredacted()
            .map_err(|err| self.redact_error(err))
    }

    fn build_unredacted(&self) -> Result<ParsedDsn, Error> {
        self.validate()?;
        let mut pairs: Vec<(&str, String)> = Vec::new();
        let mut push = |key: &'static str, value: Option<String>| {
            if let Some(value) = value {
                pairs.push((key, value));
            }
        };
        push("host", self.host.clone());
        push("port", self.port.map(|p| p.to_string()));
        push("user", self.user.clone());
        push("password", self.password.clone());
        push("dbname", self.database.clone());
        push("application_name", self.application_name.clone());

        let mut options = self
            .extra
            .iter()
            .find(|(k, _)| k == "options")
            .map(|(_, v)| v.clone())
            .unwrap_or_default();
        let mut settings = Vec::new();
        for (key, value) in &self.server_settings {
            settings.push(format!("-c {key}={}", escape_option(value)));
        }
        if !settings.is_empty() {
            if !options.is_empty() {
                options.push(' ');
            }
            options.push_str(&settings.join(" "));
        }
        if !options.is_empty() {
            pairs.push(("options", options));
        }
        for (key, value) in &self.extra {
            // a typed host or port wins over a retained multi-host value
            if key == "options"
                || (key == "host" && self.host.is_some())
                || (key == "port" && self.port.is_some())
            {
                continue;
            }
            pairs.push((key.as_str(), value.clone()));
        }

        let text = pairs
            .iter()
            .map(|(key, value)| format!("{key}={}", quote(value)))
            .collect::<Vec<_>>()
            .join(" ");
        let mut config: Config = text.parse().map_err(|_| invalid("could not be parsed"))?;
        let tls = TlsSettings {
            mode: self
                .sslmode
                .as_deref()
                .map(Mode::parse)
                .transpose()?
                .unwrap_or_default(),
            root_cert: self.sslrootcert.clone().map(Into::into),
        };
        config.ssl_mode(tls.mode.transport());
        if let Some(timeout) = self.connect_timeout.filter(|t| !t.is_zero()) {
            config.connect_timeout(timeout);
        }
        Ok(ParsedDsn {
            config,
            tls,
            secrets: Secrets(self.secrets()),
        })
    }
}

// a key in an error message, escaped and bounded so it can't carry a control character
fn shown(key: &str) -> String {
    let mut text = format!("{:?}", key.chars().take(64).collect::<String>());
    if key.chars().count() > 64 {
        text.push_str("...");
    }
    text
}

// `^[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*)*$` with at most 63 bytes per segment
fn is_guc_name(key: &str) -> bool {
    key.split('.').all(|segment| {
        let mut bytes = segment.bytes();
        segment.len() <= 63
            && bytes
                .next()
                .is_some_and(|b| b.is_ascii_alphabetic() || b == b'_')
            && bytes.all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'$')
    })
}

// NUL and the other C0 characters (tab included) are never valid in an option value
fn check_value(name: &str, value: &str) -> Result<(), Error> {
    if value.chars().any(|c| c <= '\u{1f}') {
        return Err(invalid(&format!(
            "the value of {} contains a control character",
            shown(name)
        )));
    }
    Ok(())
}

// libpq's rule for the startup `options` string: a backslash escapes the next character
fn escape_option(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for c in value.chars() {
        if c == '\\' || c.is_whitespace() {
            out.push('\\');
        }
        out.push(c);
    }
    out
}

fn split_options(options: &str) -> Vec<String> {
    let mut tokens = Vec::new();
    let mut current = String::new();
    let mut started = false;
    let mut chars = options.chars();
    while let Some(c) = chars.next() {
        if c == '\\' {
            current.push(chars.next().unwrap_or('\\'));
            started = true;
        } else if c.is_whitespace() {
            if started {
                tokens.push(std::mem::take(&mut current));
                started = false;
            }
        } else {
            current.push(c);
            started = true;
        }
    }
    if started {
        tokens.push(current);
    }
    tokens
}

/// # Errors
/// `Interface` for malformed DSNs and for an unusable `sslmode`, and the client-certificate keys.
pub fn parse(dsn: &str) -> Result<ParsedDsn, Error> {
    ConnectOptions::from_dsn(dsn)?.build()
}

type Pairs = Vec<(String, String)>;

// the URL without its query, and the query pairs decoded
fn split_url(dsn: &str) -> Result<(&str, Pairs), Error> {
    let (base, query) = match dsn.split_once('?') {
        Some((base, query)) => (base, query),
        None => (dsn, ""),
    };
    let mut pairs = Vec::new();
    for pair in query.split('&').filter(|p| !p.is_empty()) {
        let (key, value) = pair
            .split_once('=')
            .ok_or_else(|| invalid("malformed query"))?;
        pairs.push((percent_decode(key), percent_decode(value)));
    }
    Ok((base, pairs))
}

pub(crate) fn percent_decode(s: &str) -> String {
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

fn split_key_value(dsn: &str) -> Result<Pairs, Error> {
    let mut pairs = Vec::new();
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
        pairs.push((key, read_value(&mut chars)?));
    }
    Ok(pairs)
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
    use tokio_postgres::config::SslMode;

    use super::*;

    fn host(dsn: &str) -> Result<(Vec<String>, Mode), Error> {
        let parsed = parse(dsn)?;
        let hosts = parsed
            .config
            .get_hosts()
            .iter()
            .map(|h| format!("{h:?}"))
            .collect();
        Ok((hosts, parsed.tls.mode))
    }

    #[test]
    fn url_form() -> Result<(), Error> {
        let parsed = parse("postgres://u:p@localhost:5433/db")?;
        assert_eq!(parsed.config.get_user(), Some("u"));
        assert_eq!(parsed.config.get_dbname(), Some("db"));
        assert_eq!(parsed.config.get_ports(), &[5433]);
        assert_eq!(parsed.tls, TlsSettings::default());
        Ok(())
    }

    #[test]
    fn url_strips_ssl_keys_and_keeps_others() -> Result<(), Error> {
        let parsed = parse("postgresql://u@h/db?sslmode=disable&application_name=x")?;
        assert_eq!(parsed.tls.mode, Mode::Disable);
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
        assert_eq!(parsed.tls.mode, Mode::Disable);
        assert_eq!(host("host=h")?.0.len(), 1);
        Ok(())
    }

    #[test]
    fn ssl_keys_map_to_tls_settings() -> Result<(), Error> {
        let parsed = parse("postgres://u@h/db?sslmode=verify-full&sslrootcert=%2Ftmp%2Fca.pem")?;
        assert_eq!(parsed.tls.mode, Mode::VerifyFull);
        assert_eq!(parsed.tls.root_cert, Some("/tmp/ca.pem".into()));
        assert_eq!(parsed.config.get_ssl_mode(), SslMode::Require);

        let parsed = parse("host=h sslmode=verify-ca sslrootcert=/tmp/ca.pem")?;
        assert_eq!(parsed.tls.mode, Mode::VerifyCa);
        assert_eq!(parsed.config.get_ssl_mode(), SslMode::Require);

        assert_eq!(parse("host=h")?.config.get_ssl_mode(), SslMode::Prefer);
        Ok(())
    }

    #[test]
    fn unsupported_ssl_settings_error() {
        for dsn in [
            "postgres://u@h/db?sslmode=allow",
            "postgres://u@h/db?sslcert=/x",
            "host=h sslkey=/x",
            "host=h sslcrl=/x",
        ] {
            assert!(matches!(parse(dsn), Err(Error::Interface(_))), "{dsn}");
        }
    }

    #[test]
    fn options_split_into_server_settings() -> Result<(), Error> {
        let opts = ConnectOptions::from_dsn(
            "host=h options='-c search_path=a\\\\ b -ctimezone=UTC --work_mem=1MB -fsomething'",
        )?;
        assert_eq!(
            opts.server_settings,
            vec![
                ("search_path".to_owned(), "a b".to_owned()),
                ("timezone".to_owned(), "UTC".to_owned()),
                ("work_mem".to_owned(), "1MB".to_owned()),
            ]
        );
        assert_eq!(
            opts.extra,
            vec![("options".to_owned(), "-fsomething".to_owned())]
        );
        Ok(())
    }

    #[test]
    fn server_settings_are_escaped_into_startup_options() -> Result<(), Error> {
        let opts = ConnectOptions {
            host: Some("h".to_owned()),
            server_settings: vec![("search_path".to_owned(), "a b\\c".to_owned())],
            ..ConnectOptions::default()
        };
        let config = opts.build()?.config;
        assert_eq!(config.get_options(), Some("-c search_path=a\\ b\\\\c"));
        let again = ConnectOptions::from_dsn(&format!(
            "host=h options={}",
            quote(config.get_options().unwrap_or_default())
        ))?;
        assert_eq!(again.server_settings, opts.server_settings);
        Ok(())
    }

    #[test]
    fn other_keys_land_in_extra_and_survive_build() -> Result<(), Error> {
        let opts = ConnectOptions::from_dsn(
            "postgres://u@a:1,b:2/db?keepalives=0&target_session_attrs=read-write&connect_timeout=3",
        )?;
        assert_eq!(opts.host, None);
        assert_eq!(opts.connect_timeout, Some(Duration::from_secs(3)));
        assert_eq!(
            opts.extra,
            vec![
                ("host".to_owned(), "a,b".to_owned()),
                ("port".to_owned(), "1,2".to_owned()),
                ("keepalives".to_owned(), "0".to_owned()),
                ("target_session_attrs".to_owned(), "read-write".to_owned()),
            ]
        );
        let config = opts.build()?.config;
        assert_eq!(config.get_hosts().len(), 2);
        assert_eq!(config.get_ports(), &[1, 2]);
        assert!(!config.get_keepalives());
        assert_eq!(config.get_connect_timeout(), Some(&Duration::from_secs(3)));
        Ok(())
    }

    #[test]
    fn query_pairs_override_the_url_and_zero_timeout_means_none() -> Result<(), Error> {
        let opts = ConnectOptions::from_dsn("postgres://u@h:1/db?port=2&connect_timeout=0")?;
        assert_eq!(opts.port, Some(2));
        assert_eq!(opts.connect_timeout, None);
        Ok(())
    }

    #[test]
    fn typed_host_and_port_win_over_retained_multi_host_extra() -> Result<(), Error> {
        let opts = ConnectOptions {
            host: Some("c".to_owned()),
            port: Some(3),
            ..ConnectOptions::from_dsn("postgres://u@a:1,b:2/db")?
        };
        let config = opts.build()?.config;
        assert_eq!(config.get_hosts().len(), 1);
        assert_eq!(config.get_ports(), &[3]);
        Ok(())
    }

    #[test]
    fn password_never_appears_in_debug() -> Result<(), Error> {
        let opts = ConnectOptions::from_dsn("host=h password=hunter2 sslpassword=other")?;
        let shown = format!("{opts:?}");
        assert!(
            !shown.contains("hunter2") && !shown.contains("other"),
            "{shown}"
        );
        Ok(())
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

    fn with_setting(key: &str, value: &str) -> ConnectOptions {
        ConnectOptions {
            host: Some("h".to_owned()),
            server_settings: vec![(key.to_owned(), value.to_owned())],
            ..ConnectOptions::default()
        }
    }

    fn with_extra(key: &str, value: &str) -> ConnectOptions {
        ConnectOptions {
            host: Some("h".to_owned()),
            extra: vec![(key.to_owned(), value.to_owned())],
            ..ConnectOptions::default()
        }
    }

    fn message(opts: &ConnectOptions) -> Option<String> {
        match opts.build() {
            Err(Error::Interface(message)) => Some(message),
            _ => None,
        }
    }

    #[test]
    fn server_setting_keys_follow_the_guc_grammar() -> Result<(), Error> {
        for key in [
            "a\tb", "a\nb", "a\\b", "-c", "-x", "a=b", "a b", "", ".a", "a.", "a..b", "1a", "a-b",
            "a\u{0}b",
        ] {
            let opts = with_setting(key, "v");
            assert!(message(&opts).is_some(), "{key:?}");
        }
        for key in ["search_path", "app.tenant", "_x$1", "A.b_C"] {
            with_setting(key, "v").build()?;
        }
        let segment = "a".repeat(63);
        with_setting(&segment, "v").build()?;
        with_setting(&format!("{segment}.{segment}"), "v").build()?;
        let long = "a".repeat(64);
        assert!(message(&with_setting(&long, "v")).is_some());
        assert!(message(&with_setting(&format!("ok.{long}"), "v")).is_some());
        Ok(())
    }

    #[test]
    fn extra_keys_are_lowercase_and_underscore_only() -> Result<(), Error> {
        for key in [
            "Keepalives",
            "keep-alives",
            "a\n",
            "a b",
            "a=b",
            "",
            "a1",
            "-c",
            "a\tb",
        ] {
            assert!(message(&with_extra(key, "v")).is_some(), "{key:?}");
        }
        with_extra("keepalives", "0").build()?;
        with_extra("target_session_attrs", "any").build()?;
        Ok(())
    }

    #[test]
    fn invalid_key_messages_never_carry_control_characters() {
        let shown = message(&with_setting("a\nb\tc", "v")).unwrap_or_default();
        assert!(shown.contains("a\\nb\\tc"), "{shown}");
        assert!(!shown.contains('\n') && !shown.contains('\t'), "{shown}");
    }

    #[test]
    fn control_characters_in_values_are_rejected_by_key() {
        for bad in ["a\u{0}b", "a\nb", "a\u{1f}b", "a\tb", "a\rb"] {
            let secret = format!("zz{bad}zz");
            let cases = [
                (
                    "password",
                    ConnectOptions {
                        password: Some(secret.clone()),
                        ..ConnectOptions::default()
                    },
                ),
                (
                    "user",
                    ConnectOptions {
                        user: Some(secret.clone()),
                        ..ConnectOptions::default()
                    },
                ),
                (
                    "database",
                    ConnectOptions {
                        database: Some(secret.clone()),
                        ..ConnectOptions::default()
                    },
                ),
                (
                    "host",
                    ConnectOptions {
                        host: Some(secret.clone()),
                        ..ConnectOptions::default()
                    },
                ),
                (
                    "application_name",
                    ConnectOptions {
                        application_name: Some(secret.clone()),
                        ..ConnectOptions::default()
                    },
                ),
                (
                    "sslrootcert",
                    ConnectOptions {
                        sslrootcert: Some(secret.clone()),
                        ..ConnectOptions::default()
                    },
                ),
                ("work_mem", with_setting("work_mem", &secret)),
                ("sslpassword", with_extra("sslpassword", &secret)),
                ("options", with_extra("options", &secret)),
            ];
            for (name, opts) in cases {
                let text = message(&opts).unwrap_or_default();
                assert!(text.contains(name), "{name}: {text}");
                assert!(!text.contains("zz"), "{name}: {text}");
            }
        }
    }

    #[test]
    fn dsn_values_with_control_characters_are_rejected_without_the_value() {
        for dsn in [
            "host=h password='pw\tsecret'",
            "host=h options='-c work_mem=a\nsecret'",
            "postgres://u:pw%0Asecret@h/db",
            "postgres://u@h/db?sslpassword=pw%00secret",
        ] {
            let text = match parse(dsn) {
                Err(Error::Interface(message)) => message,
                other => format!("unexpected {other:?}"),
            };
            assert!(text.contains("control character"), "{dsn}: {text}");
            assert!(!text.contains("secret"), "{dsn}: {text}");
        }
    }

    #[test]
    fn dsn_keys_are_validated_too() {
        for dsn in ["host=h Keepalives=0", "postgres://u@h/db?bad-key=1"] {
            assert!(matches!(parse(dsn), Err(Error::Interface(_))), "{dsn}");
        }
    }

    #[test]
    fn long_options_map_dashes_to_underscores() -> Result<(), Error> {
        let opts = ConnectOptions::from_dsn("host=h options='--search-path=x -c work_mem=1'")?;
        assert_eq!(
            opts.server_settings,
            vec![
                ("search_path".to_owned(), "x".to_owned()),
                ("work_mem".to_owned(), "1".to_owned())
            ]
        );
        assert!(ConnectOptions::from_dsn("host=h options='--1a-b=1'").is_err());
        let opts = ConnectOptions::from_dsn("host=h options='-c search-path=x'")?;
        assert_eq!(
            opts.server_settings,
            vec![("search_path".to_owned(), "x".to_owned())]
        );
        Ok(())
    }

    #[test]
    fn debug_output_never_carries_a_secret() -> Result<(), Error> {
        let parsed = ConnectOptions::from_dsn("host=h password=Zq9pwSecret")?.build()?;
        let shown = format!("{parsed:?}");
        assert!(!shown.contains("Zq9"), "{shown}");
        assert!(shown.contains("<1 redacted>"), "{shown}");
        Ok(())
    }

    #[test]
    fn build_errors_never_carry_the_password() {
        for (sslmode, root) in [
            (Some("bogus-Zq9pw"), None),
            (Some("verify-full"), Some("/nonexistent/Zq9pw/ca.pem")),
        ] {
            let opts = ConnectOptions {
                host: Some("h".to_owned()),
                password: Some("Zq9pw".to_owned()),
                sslmode: sslmode.map(str::to_owned),
                sslrootcert: root.map(str::to_owned),
                ..ConnectOptions::default()
            };
            let text = match opts.build().and_then(|parsed| {
                parsed
                    .tls
                    .connector()
                    .map_err(|e| parsed.redact_error(e))
                    .map(|_| ())
            }) {
                Err(Error::Interface(message)) => message,
                other => format!("unexpected {other:?}"),
            };
            assert!(text.contains("***") && !text.contains("Zq9pw"), "{text}");
        }
    }

    #[test]
    fn dotted_gucs_round_trip_through_a_dsn() -> Result<(), Error> {
        let opts = ConnectOptions::from_dsn("host=h options='-c app.tenant=acme'")?;
        assert_eq!(
            opts.server_settings,
            vec![("app.tenant".to_owned(), "acme".to_owned())]
        );
        Ok(())
    }

    #[test]
    fn parse_errors_redact_every_spelling_of_the_password() {
        for dsn in [
            "postgres://u:p%40ss@h:notaport/db",
            "postgres://u:p%40ss@h/db?port=zz",
            "host=h password=p@ss port=zz",
            "host=h password='p@ss' port=zz",
            "host=h password='p@ss",
            "p@ss password=p@ss oops",
        ] {
            let text = match parse(dsn) {
                Err(Error::Interface(message)) => message,
                other => format!("unexpected {other:?}"),
            };
            assert!(
                !text.contains("p@ss") && !text.contains("p%40ss"),
                "{dsn}: {text}"
            );
        }
    }
}

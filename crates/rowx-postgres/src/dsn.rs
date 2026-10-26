use std::fmt;
use std::time::Duration;

use tokio_postgres::Config;
use tokio_postgres::config::Host;

use crate::error::Error;
use crate::tls::{Mode, TlsSettings};

pub const DEFAULT_STATEMENT_CACHE_SIZE: usize = 256;

#[derive(Debug)]
pub struct ParsedDsn {
    pub config: Config,
    pub tls: TlsSettings,
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
            "options" => self.apply_options(&value)?,
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
                token.strip_prefix("--").map(str::to_owned)
            };
            match setting {
                Some(setting) => {
                    let (key, value) = setting
                        .split_once('=')
                        .ok_or_else(|| invalid("malformed options"))?;
                    upsert(&mut self.server_settings, key, value.to_owned());
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
            if key.is_empty() || key.contains(['=', ' ']) {
                return Err(invalid("malformed server setting name"));
            }
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
            if key == "options" {
                continue;
            }
            if key.is_empty() || key.contains(['=', ' ']) {
                return Err(invalid("malformed key"));
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
        Ok(ParsedDsn { config, tls })
    }
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
}

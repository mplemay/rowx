use std::fs::File;
use std::io::BufReader;
use std::path::PathBuf;
use std::sync::Arc;

use rustls::client::WebPkiServerVerifier;
use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::crypto::{CryptoProvider, ring, verify_tls12_signature, verify_tls13_signature};
use rustls::pki_types::{CertificateDer, ServerName, UnixTime};
use rustls::{
    CertificateError, ClientConfig, DigitallySignedStruct, Error as TlsError, RootCertStore,
    SignatureScheme,
};
use tokio_postgres::config::SslMode;
use tokio_postgres_rustls::MakeRustlsConnect;

use crate::error::Error;

/// libpq's `sslmode`, including the modes tokio-postgres cannot express.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Mode {
    Disable,
    #[default]
    Prefer,
    Require,
    VerifyCa,
    VerifyFull,
}

impl Mode {
    /// # Errors
    /// `Interface` for a value libpq does not accept, or `allow`, which has no TLS equivalent here.
    pub fn parse(value: &str) -> Result<Self, Error> {
        match value {
            "disable" => Ok(Self::Disable),
            "prefer" => Ok(Self::Prefer),
            "require" => Ok(Self::Require),
            "verify-ca" => Ok(Self::VerifyCa),
            "verify-full" => Ok(Self::VerifyFull),
            _ => Err(Error::Interface(format!(
                "unsupported sslmode '{value}'; expected disable, prefer, require, verify-ca or verify-full"
            ))),
        }
    }

    // verify-ca and verify-full need an encrypted channel, so tokio-postgres only ever sees Require
    #[must_use]
    pub const fn transport(self) -> SslMode {
        match self {
            Self::Disable => SslMode::Disable,
            Self::Prefer => SslMode::Prefer,
            Self::Require | Self::VerifyCa | Self::VerifyFull => SslMode::Require,
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct TlsSettings {
    pub mode: Mode,
    pub root_cert: Option<PathBuf>,
}

impl TlsSettings {
    /// One connector serves both the connection and its cancel requests.
    ///
    /// # Errors
    /// `Interface` when `sslrootcert` cannot be read or holds no certificates.
    pub fn connector(&self) -> Result<MakeRustlsConnect, Error> {
        // the provider is passed explicitly, so no process-wide default is ever installed
        let provider = Arc::new(ring::default_provider());
        let verifier = self.verifier(&provider)?;
        let config = ClientConfig::builder_with_provider(provider)
            .with_safe_default_protocol_versions()
            .map_err(|e| Error::Interface(format!("TLS setup failed: {e}")))?
            .dangerous()
            .with_custom_certificate_verifier(verifier)
            .with_no_client_auth();
        Ok(MakeRustlsConnect::new(config))
    }

    fn verifier(
        &self,
        provider: &Arc<CryptoProvider>,
    ) -> Result<Arc<dyn ServerCertVerifier>, Error> {
        let chain_only = match self.mode {
            Mode::Disable | Mode::Prefer => false,
            Mode::Require => self.root_cert.is_some(),
            Mode::VerifyCa => true,
            Mode::VerifyFull => {
                return Ok(self.webpki(provider)?);
            }
        };
        if !chain_only {
            return Ok(Arc::new(AcceptAny {
                provider: Arc::clone(provider),
            }));
        }
        Ok(Arc::new(IgnoreHostname {
            inner: self.webpki(provider)?,
        }))
    }

    fn webpki(&self, provider: &Arc<CryptoProvider>) -> Result<Arc<WebPkiServerVerifier>, Error> {
        let roots = match &self.root_cert {
            Some(path) => load_roots(path)?,
            None => RootCertStore::from_iter(webpki_roots::TLS_SERVER_ROOTS.iter().cloned()),
        };
        WebPkiServerVerifier::builder_with_provider(Arc::new(roots), Arc::clone(provider))
            .build()
            .map_err(|e| Error::Interface(format!("TLS setup failed: {e}")))
    }
}

fn load_roots(path: &std::path::Path) -> Result<RootCertStore, Error> {
    let unreadable = |what: &dyn std::fmt::Display| {
        Error::Interface(format!(
            "could not read sslrootcert {}: {what}",
            path.display()
        ))
    };
    let file = File::open(path).map_err(|e| unreadable(&e))?;
    let certs = rustls_pemfile::certs(&mut BufReader::new(file))
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| unreadable(&e))?;
    let mut roots = RootCertStore::empty();
    let (added, _) = roots.add_parsable_certificates(certs);
    if added == 0 {
        return Err(unreadable(&"no certificates found"));
    }
    Ok(roots)
}

// libpq's prefer/require: the channel is encrypted but the server is not authenticated
#[derive(Debug)]
struct AcceptAny {
    provider: Arc<CryptoProvider>,
}

impl ServerCertVerifier for AcceptAny {
    fn verify_server_cert(
        &self,
        _end_entity: &CertificateDer<'_>,
        _intermediates: &[CertificateDer<'_>],
        _server_name: &ServerName<'_>,
        _ocsp_response: &[u8],
        _now: UnixTime,
    ) -> Result<ServerCertVerified, TlsError> {
        Ok(ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, TlsError> {
        verify_tls12_signature(
            message,
            cert,
            dss,
            &self.provider.signature_verification_algorithms,
        )
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, TlsError> {
        verify_tls13_signature(
            message,
            cert,
            dss,
            &self.provider.signature_verification_algorithms,
        )
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.provider
            .signature_verification_algorithms
            .supported_schemes()
    }
}

// verify-ca: the whole chain is checked, and only a name mismatch is forgiven
#[derive(Debug)]
struct IgnoreHostname {
    inner: Arc<WebPkiServerVerifier>,
}

impl ServerCertVerifier for IgnoreHostname {
    fn verify_server_cert(
        &self,
        end_entity: &CertificateDer<'_>,
        intermediates: &[CertificateDer<'_>],
        server_name: &ServerName<'_>,
        ocsp_response: &[u8],
        now: UnixTime,
    ) -> Result<ServerCertVerified, TlsError> {
        match self.inner.verify_server_cert(
            end_entity,
            intermediates,
            server_name,
            ocsp_response,
            now,
        ) {
            Err(TlsError::InvalidCertificate(
                CertificateError::NotValidForName | CertificateError::NotValidForNameContext { .. },
            )) => Ok(ServerCertVerified::assertion()),
            other => other,
        }
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, TlsError> {
        self.inner.verify_tls12_signature(message, cert, dss)
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, TlsError> {
        self.inner.verify_tls13_signature(message, cert, dss)
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.inner.supported_verify_schemes()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn modes_map_to_transport() -> Result<(), Error> {
        for (text, mode, transport) in [
            ("disable", Mode::Disable, SslMode::Disable),
            ("prefer", Mode::Prefer, SslMode::Prefer),
            ("require", Mode::Require, SslMode::Require),
            ("verify-ca", Mode::VerifyCa, SslMode::Require),
            ("verify-full", Mode::VerifyFull, SslMode::Require),
        ] {
            assert_eq!(Mode::parse(text)?, mode);
            assert_eq!(mode.transport(), transport);
        }
        assert!(Mode::parse("allow").is_err());
        Ok(())
    }

    #[test]
    fn every_mode_builds_a_connector() {
        for mode in [
            Mode::Disable,
            Mode::Prefer,
            Mode::Require,
            Mode::VerifyCa,
            Mode::VerifyFull,
        ] {
            let settings = TlsSettings {
                mode,
                root_cert: None,
            };
            assert!(settings.connector().is_ok(), "{mode:?}");
        }
    }

    #[test]
    fn missing_root_cert_is_an_interface_error() {
        let settings = TlsSettings {
            mode: Mode::VerifyFull,
            root_cert: Some(PathBuf::from("/nonexistent/ca.pem")),
        };
        assert!(matches!(settings.connector(), Err(Error::Interface(_))));
    }

    #[test]
    fn root_cert_without_certificates_is_an_interface_error()
    -> Result<(), Box<dyn std::error::Error>> {
        let path = std::env::temp_dir().join(format!("rowx-empty-ca-{}.pem", std::process::id()));
        std::fs::write(&path, "not a certificate\n")?;
        let settings = TlsSettings {
            mode: Mode::Require,
            root_cert: Some(path.clone()),
        };
        let result = settings.connector();
        std::fs::remove_file(&path)?;
        assert!(matches!(result, Err(Error::Interface(_))));
        Ok(())
    }
}

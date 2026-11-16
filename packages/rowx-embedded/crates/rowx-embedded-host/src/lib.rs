pub mod classify;
pub mod engine;
pub mod error;
pub mod instance;
pub mod owner;
pub mod proxy;
pub mod runtime;
pub mod seed;
pub mod slot;
pub mod snapshot;
pub mod sqlscan;
pub mod wire;

use std::io::Write;

// stderr tracing for fallbacks and dropped connections, enabled with ROWX_EMBEDDED_LOG=1
pub(crate) fn log(message: &str) {
    if std::env::var_os("ROWX_EMBEDDED_LOG").is_some() {
        let _ = writeln!(std::io::stderr(), "[rowx-embedded] {message}");
    }
}

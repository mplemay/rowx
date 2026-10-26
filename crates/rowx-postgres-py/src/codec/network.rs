use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBytes, PyModule};
use rowx_postgres::Type;
use rowx_postgres::exec::Param;
use rowx_postgres::wire::network::{self, Addr, Network};

use crate::codec::extract::{IpKind, PyParam};
use crate::errors::decode_error;

static IPADDRESS: PyOnceLock<Py<PyModule>> = PyOnceLock::new();

fn module(py: Python<'_>) -> PyResult<&Bound<'_, PyModule>> {
    let module =
        IPADDRESS.get_or_try_init(py, || Ok::<_, PyErr>(py.import("ipaddress")?.unbind()))?;
    Ok(module.bind(py))
}

/// # Errors
/// An import error when `ipaddress` cannot be loaded.
pub fn class<'py>(py: Python<'py>, name: &str) -> PyResult<Bound<'py, PyAny>> {
    module(py)?.getattr(name)
}

/// `inet` always decodes to an interface and `cidr` to a network.
///
/// # Errors
/// `DecodeError` when the bytes are not a well-formed `inet` or `cidr`.
pub fn decode<'py>(py: Python<'py>, ty: &Type, raw: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    let value = network::decode(raw)
        .map_err(|e| decode_error(py, &format!("{} is malformed: {e}", ty.name())))?;
    let (v4, bytes) = match &value.addr {
        Addr::V4(bytes) => (true, bytes.as_slice()),
        Addr::V6(bytes) => (false, bytes.as_slice()),
    };
    let name = match (*ty == Type::CIDR, v4) {
        (true, true) => "IPv4Network",
        (true, false) => "IPv6Network",
        (false, true) => "IPv4Interface",
        (false, false) => "IPv6Interface",
    };
    class(py, name)?.call1(((PyBytes::new(py, bytes), value.bits),))
}

// `ip` is the address object, `prefix` its prefix length
fn parts(py: Python<'_>, ip: &Bound<'_, PyAny>, prefix: u8) -> PyResult<(Addr, u8)> {
    // a zone id has no wire form, so it would be silently dropped
    if ip.getattr("scope_id").is_ok_and(|scope| !scope.is_none()) {
        return Err(crate::errors::encode_error(
            py,
            "an IPv6 address with a zone id cannot be sent as inet or cidr",
        ));
    }
    let packed = ip.getattr("packed")?.extract::<Vec<u8>>()?;
    let addr = match <[u8; 4]>::try_from(packed.as_slice()) {
        Ok(bytes) => Addr::V4(bytes),
        Err(_) => Addr::V6(<[u8; 16]>::try_from(packed.as_slice()).map_err(|_| {
            crate::errors::encode_error(py, "an address is neither 4 nor 16 bytes")
        })?),
    };
    Ok((addr, prefix))
}

/// Reads an `ipaddress` object into its address, prefix and kind; `None` when it is not one.
///
/// # Errors
/// `EncodeError` for a zone id.
pub fn read(py: Python<'_>, obj: &Bound<'_, PyAny>) -> PyResult<Option<(IpKind, Addr, u8)>> {
    // an interface subclasses its address type, so it is checked first
    for (name, kind) in [
        ("IPv4Interface", IpKind::Interface),
        ("IPv6Interface", IpKind::Interface),
        ("IPv4Address", IpKind::Address),
        ("IPv6Address", IpKind::Address),
        ("IPv4Network", IpKind::Network),
        ("IPv6Network", IpKind::Network),
    ] {
        if !obj.is_instance(&class(py, name)?)? {
            continue;
        }
        let (ip, prefix) = match kind {
            // an interface is itself an address and keeps the zone id that its `ip` drops
            IpKind::Interface => (
                obj.clone(),
                obj.getattr("network")?.getattr("prefixlen")?.extract()?,
            ),
            IpKind::Address => (obj.clone(), if name == "IPv4Address" { 32 } else { 128 }),
            IpKind::Network => (
                obj.getattr("network_address")?,
                obj.getattr("prefixlen")?.extract()?,
            ),
        };
        let (addr, prefix) = parts(py, &ip, prefix)?;
        return Ok(Some((kind, addr, prefix)));
    }
    Ok(None)
}

// parses with `ipaddress`, so every spelling Python accepts works and anything else is an error
fn parse(text: &str, cidr: bool) -> Result<Network, String> {
    Python::attach(|py| {
        let parsed = if cidr {
            class(py, "ip_network").and_then(|f| {
                let kwargs = pyo3::types::PyDict::new(py);
                kwargs.set_item("strict", true)?;
                f.call((text,), Some(&kwargs))
            })
        } else {
            class(py, "ip_interface").and_then(|f| f.call1((text,)))
        }
        .map_err(|e| {
            format!(
                "{text:?} is not a valid {}: {e}",
                if cidr { "cidr" } else { "inet" }
            )
        })?;
        match read(py, &parsed) {
            Ok(Some((_, addr, bits))) => Ok(Network {
                addr,
                bits,
                is_cidr: cidr,
            }),
            Ok(None) => Err(format!("{text:?} is not a valid network")),
            Err(e) => Err(e.to_string()),
        }
    })
}

/// # Errors
/// A description of why the value cannot be encoded as `inet` or `cidr`.
pub fn encode(ty: &Type, param: &PyParam) -> Result<Param, String> {
    let cidr = *ty == Type::CIDR;
    let network = match param {
        PyParam::Ip { addr, bits, .. } => Network {
            addr: *addr,
            bits: *bits,
            is_cidr: cidr,
        },
        PyParam::Str(text) => parse(text, cidr)?,
        _ => {
            return Err(format!(
                "a Python {} cannot be encoded as {}",
                param.type_name(),
                ty.name()
            ));
        }
    };
    network::encode(&network)
        .map(Param::binary)
        .map_err(|e| e.to_string())
}

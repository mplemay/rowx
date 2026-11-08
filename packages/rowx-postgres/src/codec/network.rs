use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBytes, PyInt, PyModule, PyType};
use rowx_postgres::Type;
use rowx_postgres::exec::Param;
use rowx_postgres::wire::network::{self, Addr, Network};

use crate::codec::exact::{field, instance_dict, is_a};
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

fn bad(py: Python<'_>, what: &str) -> PyErr {
    crate::errors::encode_error(py, what)
}

// The address of an `ipaddress` address (or interface) read from its `_ip` and `_scope_id` slots through the base
// class, so a subclass's `packed` or `scope_id` property changes nothing.
fn address(py: Python<'_>, obj: &Bound<'_, PyAny>, v4: bool) -> PyResult<Addr> {
    let cls = class(py, if v4 { "IPv4Address" } else { "IPv6Address" })?.cast_into::<PyType>()?;
    if !is_a(obj, &cls)? {
        return Err(bad(
            py,
            "an ipaddress object holds a value that is not an address",
        ));
    }
    // a zone id has no wire form, so it would be silently dropped
    if !v4 && !field(obj, &cls, "_scope_id")?.is_none() {
        return Err(bad(
            py,
            "an IPv6 address with a zone id cannot be sent as inet or cidr",
        ));
    }
    let raw = field(obj, &cls, "_ip")?;
    let int = raw
        .cast::<PyInt>()
        .map_err(|_| bad(py, "an ipaddress object has a malformed address"))?;
    let value = int.extract::<u128>()?;
    if v4 {
        let value = u32::try_from(value)
            .map_err(|_| bad(py, "an ipaddress object has a malformed address"))?;
        Ok(Addr::V4(value.to_be_bytes()))
    } else {
        Ok(Addr::V6(value.to_be_bytes()))
    }
}

// the prefix length from the instance state `ipaddress` sets in `__init__`, not from the `prefixlen` property
fn prefix(py: Python<'_>, dict: &Bound<'_, PyAny>) -> PyResult<u8> {
    dict.get_item("_prefixlen")?
        .cast::<PyInt>()
        .map_err(|_| bad(py, "an ipaddress object has a malformed prefix length"))?
        .extract::<u8>()
        .map_err(|_| bad(py, "an ipaddress object has a malformed prefix length"))
}

/// Reads an `ipaddress` object into its address, prefix and kind; `None` when it is not one. The object is
/// recognised by its real type and read through the base classes' slots and instance state.
///
/// # Errors
/// `EncodeError` for a zone id.
pub fn read(py: Python<'_>, obj: &Bound<'_, PyAny>) -> PyResult<Option<(IpKind, Addr, u8)>> {
    // an interface subclasses its address type, so it is checked first
    for (name, kind, v4) in [
        ("IPv4Interface", IpKind::Interface, true),
        ("IPv6Interface", IpKind::Interface, false),
        ("IPv4Address", IpKind::Address, true),
        ("IPv6Address", IpKind::Address, false),
        ("IPv4Network", IpKind::Network, true),
        ("IPv6Network", IpKind::Network, false),
    ] {
        let cls = class(py, name)?.cast_into::<PyType>()?;
        if !is_a(obj, &cls)? {
            continue;
        }
        let (addr, bits) = match kind {
            IpKind::Interface => (
                address(py, obj, v4)?,
                prefix(py, &instance_dict(obj, &cls)?)?,
            ),
            IpKind::Address => (address(py, obj, v4)?, if v4 { 32 } else { 128 }),
            IpKind::Network => {
                let dict = instance_dict(obj, &cls)?;
                (
                    address(py, &dict.get_item("network_address")?, v4)?,
                    prefix(py, &dict)?,
                )
            }
        };
        return Ok(Some((kind, addr, bits)));
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

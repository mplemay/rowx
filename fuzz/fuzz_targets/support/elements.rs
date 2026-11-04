// one decoder per wire family, picked by a selector byte, so an array or a range reaches every element decoder
use rowx_postgres::wire::geometric::{self, Kind};
use rowx_postgres::wire::{bits, cube, hstore, ltree, mac, money, network, numeric, range, temporal};

pub(crate) fn decode(selector: u8, raw: &[u8]) {
    match selector % 24 {
        0 => drop(temporal::decode_date(raw)),
        1 => drop(temporal::decode_time(raw)),
        2 => drop(temporal::decode_timetz(raw)),
        3 => drop(temporal::decode_timestamp("timestamp", raw)),
        4 => drop(temporal::decode_timestamp("timestamptz", raw)),
        5 => {
            drop(temporal::decode_interval(raw));
            drop(temporal::decode_span(raw));
        }
        6 => {
            if let Ok(value) = numeric::decode(raw) {
                let _ = value.to_decimal_string();
                let _ = value.encode();
            }
        }
        7 => drop(money::decode(raw)),
        8 => drop(network::decode(raw)),
        9 => drop(mac::decode(raw, mac::MAC_LEN)),
        10 => drop(mac::decode(raw, mac::MAC8_LEN)),
        11 => drop(bits::decode(raw)),
        12 => drop(geometric::decode(Kind::Point, raw)),
        13 => drop(geometric::decode(Kind::Line, raw)),
        14 => drop(geometric::decode(Kind::LineSegment, raw)),
        15 => drop(geometric::decode(Kind::Box, raw)),
        16 => drop(geometric::decode(Kind::Path, raw)),
        17 => drop(geometric::decode(Kind::Polygon, raw)),
        18 => drop(geometric::decode(Kind::Circle, raw)),
        19 => drop(hstore::decode(raw)),
        20 => drop(ltree::decode(raw, "ltree")),
        21 => drop(ltree::decode(raw, "lquery")),
        22 => drop(cube::decode(raw)),
        _ => drop(range::decode(raw)),
    }
}

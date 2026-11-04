// wire codecs take untrusted bytes: no panicking index, slice or arithmetic (this comment also keeps `#![` off line 1 for the prek shebang check, so do not delete it)
#![deny(clippy::indexing_slicing, clippy::arithmetic_side_effects)]

pub mod array;
pub mod bits;
pub mod cube;
pub mod geometric;
pub mod hstore;
pub mod ltree;
pub mod mac;
pub mod money;
pub mod network;
pub mod numeric;
pub mod range;
pub mod temporal;
pub mod uuid;

use rowx_postgres::exec::Param;
use rowx_postgres::wire::array;
use rowx_postgres::{Kind, Type};

use crate::codec::encode_one;
use crate::codec::extract::PyParam;
use crate::codec::uuid;

const MAX_DIMS: usize = 6;

// the lengths of each level, taken from the first element at every depth
fn dims_of(items: &[PyParam]) -> Vec<usize> {
    let mut dims = vec![items.len()];
    let mut level = items;
    while let Some(PyParam::Seq(inner, ..)) = level.first() {
        dims.push(inner.len());
        level = inner;
    }
    dims
}

// flattens row-major, rejecting any level whose shape differs from `dims`
fn flatten<'a>(
    items: &'a [PyParam],
    dims: &[usize],
    out: &mut Vec<&'a PyParam>,
) -> Result<(), String> {
    let Some((&len, rest)) = dims.split_first() else {
        return Err("the nested lists are ragged".to_owned());
    };
    if items.len() != len {
        return Err("the nested lists are ragged".to_owned());
    }
    for item in items {
        match (item, rest.is_empty()) {
            (PyParam::Seq(inner, ..), false) => flatten(inner, rest, out)?,
            (PyParam::Seq(..), true) | (_, false) => {
                return Err("the nested lists are ragged".to_owned());
            }
            (leaf, true) => out.push(leaf),
        }
    }
    Ok(())
}

fn raw_elements(elements: &[Param]) -> Vec<Option<&[u8]>> {
    elements.iter().map(Param::value).collect()
}

/// Binary when every element encodes as binary, otherwise a text literal.
///
/// # Errors
/// A description of why the value cannot be encoded as an array of `ty`'s element.
pub fn encode(ty: &Type, param: &PyParam) -> Result<Param, String> {
    let Kind::Array(elem_ty) = ty.kind() else {
        return Err(format!("{} is not an array type", ty.name()));
    };
    let PyParam::Seq(items, ..) = param else {
        return Err(format!(
            "a Python {} cannot be encoded as {}",
            param.type_name(),
            ty.name()
        ));
    };
    let dims = dims_of(items);
    if dims.len() > MAX_DIMS {
        return Err(format!("the list nests deeper than {MAX_DIMS} levels"));
    }
    let mut leaves = Vec::new();
    flatten(items, &dims, &mut leaves)?;
    // an empty list at any level makes the whole array empty, once the shape is known to be regular
    if dims.contains(&0) {
        let empty = array::encode(elem_ty.oid(), &[], &[]).map_err(|e| e.to_string())?;
        return Ok(Param::binary(empty));
    }
    let mut elements = leaves
        .iter()
        .enumerate()
        .map(|(index, leaf)| {
            encode_one(elem_ty, leaf).map_err(|msg| format!("element {index}: {msg}"))
        })
        .collect::<Result<Vec<_>, _>>()?;
    if elements.iter().any(Param::is_text) {
        // uuid is the only element type that mixes forms (`UUID` binary, `str` text), and the
        // literal needs every element as text; other binary elements are already their text bytes
        for (element, leaf) in elements.iter_mut().zip(&leaves) {
            if let PyParam::Uuid(bytes) = leaf {
                *element = Param::text(uuid::to_text(bytes).into_bytes());
            }
        }
        let literal = array::encode_literal(&dims, &raw_elements(&elements));
        return Ok(Param::text(literal.into_bytes()));
    }
    array::encode(elem_ty.oid(), &dims, &raw_elements(&elements))
        .map(Param::binary)
        .map_err(|e| e.to_string())
}

use rowx_pg::exec::Param;
use rowx_pg::{Kind, Type};

use crate::codec::encode_one;
use crate::codec::extract::PyParam;

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

fn push_literal(out: &mut String, elements: &mut std::slice::Iter<'_, Param>, dims: &[usize]) {
    let Some((&len, rest)) = dims.split_first() else {
        return;
    };
    out.push('{');
    for index in 0..len {
        if index > 0 {
            out.push(',');
        }
        if rest.is_empty() {
            match elements.next().and_then(Param::value) {
                None => out.push_str("NULL"),
                Some(raw) => {
                    out.push('"');
                    for ch in String::from_utf8_lossy(raw).chars() {
                        if matches!(ch, '"' | '\\') {
                            out.push('\\');
                        }
                        out.push(ch);
                    }
                    out.push('"');
                }
            }
        } else {
            push_literal(out, elements, rest);
        }
    }
    out.push('}');
}

fn binary(elem_oid: u32, dims: &[usize], elements: &[Param]) -> Result<Vec<u8>, String> {
    let too_big = || "the array is too large".to_owned();
    let has_null = elements.iter().any(|e| e.value().is_none());
    let mut out = Vec::new();
    out.extend_from_slice(
        &i32::try_from(dims.len())
            .map_err(|_| too_big())?
            .to_be_bytes(),
    );
    out.extend_from_slice(&i32::from(has_null).to_be_bytes());
    out.extend_from_slice(&elem_oid.to_be_bytes());
    for &len in dims {
        out.extend_from_slice(&i32::try_from(len).map_err(|_| too_big())?.to_be_bytes());
        out.extend_from_slice(&1_i32.to_be_bytes());
    }
    for element in elements {
        match element.value() {
            None => out.extend_from_slice(&(-1_i32).to_be_bytes()),
            Some(raw) => {
                out.extend_from_slice(
                    &i32::try_from(raw.len())
                        .map_err(|_| too_big())?
                        .to_be_bytes(),
                );
                out.extend_from_slice(raw);
            }
        }
    }
    Ok(out)
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
    // an empty list at any level makes the whole array empty
    let dims = dims_of(items);
    if dims.contains(&0) {
        return Ok(Param::binary(binary(elem_ty.oid(), &[], &[])?));
    }
    if dims.len() > MAX_DIMS {
        return Err(format!("the list nests deeper than {MAX_DIMS} levels"));
    }
    let mut leaves = Vec::new();
    flatten(items, &dims, &mut leaves)?;
    let elements = leaves
        .into_iter()
        .enumerate()
        .map(|(index, leaf)| {
            encode_one(elem_ty, leaf).map_err(|msg| format!("element {index}: {msg}"))
        })
        .collect::<Result<Vec<_>, _>>()?;
    if elements.iter().any(Param::is_text) {
        let mut literal = String::new();
        push_literal(&mut literal, &mut elements.iter(), &dims);
        return Ok(Param::text(literal.into_bytes()));
    }
    binary(elem_ty.oid(), &dims, &elements).map(Param::binary)
}

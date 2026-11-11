//! Reconstruct objects from raw sub-values, never serde_json's private ValueVisitor keys.
use crate::{Result, error::invalid};
use serde::{
    Deserialize, Deserializer,
    de::{Error, MapAccess, Visitor},
};
use serde_json::{Map, Number, Value, value::RawValue};
use std::collections::BTreeMap;
struct RawFields<'a>(BTreeMap<String, &'a RawValue>);
impl<'de> Deserialize<'de> for RawFields<'de> {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> std::result::Result<Self, D::Error> {
        struct FieldsVisitor;
        impl<'de> Visitor<'de> for FieldsVisitor {
            type Value = RawFields<'de>;
            fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str("an object with unique keys")
            }
            fn visit_map<M: MapAccess<'de>>(
                self,
                mut map: M,
            ) -> std::result::Result<Self::Value, M::Error> {
                let mut fields = BTreeMap::new();
                while let Some((key, value)) = map.next_entry::<String, &'de RawValue>()? {
                    if fields.insert(key, value).is_some() {
                        return Err(M::Error::custom("duplicate JSON key"));
                    }
                }
                Ok(RawFields(fields))
            }
        }
        deserializer.deserialize_map(FieldsVisitor)
    }
}

pub fn parse_json_value(input: &[u8], max_bytes: usize) -> Result<Value> {
    if input.len() > max_bytes {
        return Err(invalid("JSON byte budget"));
    }
    let raw: &RawValue = serde_json::from_slice(input)?;
    decode(raw, 0)
}
fn decode(raw: &RawValue, depth: usize) -> Result<Value> {
    if depth > 128 {
        return Err(invalid("JSON nesting depth"));
    }
    let text = raw.get();
    Ok(match text.as_bytes().first() {
        Some(b'{') => {
            let RawFields(fields) = serde_json::from_str(text)?;
            let mut object = Map::new();
            for (key, value) in fields {
                object.insert(key, decode(value, depth + 1)?);
            }
            Value::Object(object)
        }
        Some(b'[') => {
            let values: Vec<&RawValue> = serde_json::from_str(text)?;
            Value::Array(
                values
                    .into_iter()
                    .map(|value| decode(value, depth + 1))
                    .collect::<Result<_>>()?,
            )
        }
        Some(b'"') => Value::String(serde_json::from_str(text)?),
        Some(b't') => Value::Bool(true),
        Some(b'f') => Value::Bool(false),
        Some(b'n') => Value::Null,
        Some(_) => {
            let number = text.parse::<Number>()?;
            if text.bytes().any(|b| b".eE".contains(&b))
                && !number.as_f64().is_some_and(f64::is_finite)
            {
                return Err(invalid("nonfinite JSON float"));
            }
            Value::Number(number)
        }
        None => return Err(invalid("JSON value")),
    })
}

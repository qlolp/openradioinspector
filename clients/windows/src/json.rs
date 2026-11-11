//! Validation only: never semantically decode/reencode transport JSON.
use serde::{
    Deserialize, Deserializer,
    de::{Error, MapAccess, Visitor},
};
use serde_json::value::RawValue;
use std::collections::HashSet;

pub(crate) fn object(raw: &str, maximum: usize) -> Result<(), String> {
    if raw.len() > maximum {
        return Err("JSON body exceeds client limit".into());
    }
    let value: &RawValue = serde_json::from_str(raw).map_err(|_| "Invalid JSON body")?;
    if !value.get().starts_with('{') {
        return Err("Core controls require a JSON object".into());
    }
    validate(value, 0).map_err(|_| "Invalid JSON keys, floats or nesting".into())
}
fn validate(raw: &RawValue, depth: usize) -> Result<(), serde_json::Error> {
    if depth > 128 {
        return Err(<serde_json::Error as serde::de::Error>::custom(
            "JSON nesting",
        ));
    }
    let text = raw.get();
    match text.as_bytes()[0] {
        b'{' => {
            let Fields(values) = serde_json::from_str::<Fields<'_>>(text)?;
            for item in values {
                validate(item, depth + 1)?;
            }
        }
        b'[' => {
            let values: Vec<&RawValue> = serde_json::from_str(text)?;
            for item in values {
                validate(item, depth + 1)?;
            }
        }
        b'-' | b'0'..=b'9'
            if text.bytes().any(|byte| b".eE".contains(&byte))
                && !text.parse::<f64>().is_ok_and(f64::is_finite) =>
        {
            return Err(<serde_json::Error as serde::de::Error>::custom(
                "nonfinite JSON float",
            ));
        }
        _ => {}
    }
    Ok(())
}
struct Fields<'a>(Vec<&'a RawValue>);
impl<'de> Deserialize<'de> for Fields<'de> {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct FieldsVisitor;
        impl<'de> Visitor<'de> for FieldsVisitor {
            type Value = Fields<'de>;
            fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str("unique JSON fields")
            }
            fn visit_map<M: MapAccess<'de>>(self, mut map: M) -> Result<Self::Value, M::Error> {
                let mut names = HashSet::new();
                let mut values = Vec::new();
                while let Some((key, value)) = map.next_entry::<String, &'de RawValue>()? {
                    if !names.insert(key) {
                        return Err(M::Error::custom("duplicate JSON key"));
                    }
                    values.push(value);
                }
                Ok(Fields(values))
            }
        }
        deserializer.deserialize_map(FieldsVisitor)
    }
}

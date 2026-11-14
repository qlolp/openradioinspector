use crate::{MAX_JSON_BYTES, MonitoringError, Result};
use serde_json::Value;
const VOLATILE: [&str; 5] = [
    "sequence",
    "connection_id",
    "segment_id",
    "revision",
    "tune_sequence",
];
/// Preserve extension values exactly; remove only the Python volatile key set.
pub fn stable(value: &Value) -> Result<Value> {
    let _ = python_canonical_json(value)?;
    fn filter(value: &Value) -> Value {
        match value {
            Value::Object(map) => Value::Object(
                map.iter()
                    .filter(|(key, _)| !VOLATILE.contains(&key.as_str()))
                    .map(|(key, value)| (key.clone(), filter(value)))
                    .collect(),
            ),
            Value::Array(items) => Value::Array(items.iter().map(filter).collect()),
            _ => value.clone(),
        }
    }
    Ok(filter(value))
}
/// Python json.dumps(sort_keys=True, ensure_ascii=True, allow_nan=False)
/// with its default comma/colon spaces and shortest IEEE-754 float repr.
pub fn python_canonical_json(value: &Value) -> Result<String> {
    let mut output = String::new();
    write(value, &mut output, 0)?;
    Ok(output)
}
fn push(output: &mut String, text: &str) -> Result<()> {
    if output.len().saturating_add(text.len()) > MAX_JSON_BYTES {
        return Err(MonitoringError::LimitExceeded("JSON bytes"));
    }
    output.push_str(text);
    Ok(())
}
fn string(value: &str, output: &mut String) -> Result<()> {
    push(output, "\"")?;
    for ch in value.chars() {
        match ch {
            '"' => push(output, "\\\"")?,
            '\\' => push(output, "\\\\")?,
            '\u{8}' => push(output, "\\b")?,
            '\u{c}' => push(output, "\\f")?,
            '\n' => push(output, "\\n")?,
            '\r' => push(output, "\\r")?,
            '\t' => push(output, "\\t")?,
            ch if (' '..='~').contains(&ch) => {
                let mut bytes = [0; 4];
                push(output, ch.encode_utf8(&mut bytes))?;
            }
            ch => {
                let mut units = [0; 2];
                for unit in ch.encode_utf16(&mut units) {
                    push(output, &format!("\\u{unit:04x}"))?;
                }
            }
        }
    }
    push(output, "\"")
}
fn write(value: &Value, output: &mut String, depth: usize) -> Result<()> {
    if depth > 64 {
        return Err(MonitoringError::LimitExceeded("JSON depth"));
    }
    match value {
        Value::Null => push(output, "null"),
        Value::Bool(value) => push(output, if *value { "true" } else { "false" }),
        Value::String(value) => string(value, output),
        Value::Number(value) => {
            let token = value.to_string();
            if !token.contains(['.', 'e', 'E']) {
                let digits = token.strip_prefix('-').unwrap_or(&token);
                if digits.is_empty() || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
                    return Err(MonitoringError::Json("invalid integer".into()));
                }
                return push(output, if token == "-0" { "0" } else { &token });
            }
            let number = token
                .parse::<f64>()
                .map_err(|_| MonitoringError::Json("invalid number".into()))?;
            if !number.is_finite() {
                return Err(MonitoringError::Json("nonfinite number".into()));
            }
            let representation = format!("{number:?}");
            if let Some((coefficient, exponent)) = representation.split_once('e') {
                let exponent: i32 = exponent
                    .parse()
                    .map_err(|_| MonitoringError::Json("invalid exponent".into()))?;
                push(output, &format!("{coefficient}e{exponent:+03}"))
            } else {
                push(output, &representation)
            }
        }
        Value::Array(items) => {
            push(output, "[")?;
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    push(output, ", ")?;
                }
                write(item, output, depth + 1)?;
            }
            push(output, "]")
        }
        Value::Object(map) => {
            push(output, "{")?;
            let mut keys: Vec<_> = map.keys().collect();
            keys.sort();
            for (index, key) in keys.into_iter().enumerate() {
                if index > 0 {
                    push(output, ", ")?;
                }
                string(key, output)?;
                push(output, ": ")?;
                write(&map[key], output, depth + 1)?;
            }
            push(output, "}")
        }
    }
}

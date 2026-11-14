use crate::{GuardError, Result};
use ori_contracts::{RestoreKeys, UtcTimestamp};
use serde::{Serialize, de::DeserializeOwned};
use serde_json::{Map, Value};

pub(crate) fn invalid(message: impl Into<String>) -> GuardError {
    GuardError::Invalid(message.into())
}
pub(crate) fn fields<'a>(
    value: &'a Value,
    allowed: &[&str],
    exact: bool,
) -> Result<&'a Map<String, Value>> {
    let map = value
        .as_object()
        .ok_or_else(|| invalid("Тело запроса должно быть JSON-объектом"))?;
    if map.keys().any(|k| !allowed.contains(&k.as_str())) || exact && map.len() != allowed.len() {
        return Err(invalid("Неизвестное или отсутствующее поле запроса"));
    }
    Ok(map)
}
pub(crate) fn text(value: &Value, key: &str, maximum: usize) -> Result<String> {
    text_with_label(value, key, key, maximum)
}
pub(crate) fn text_with_label(
    value: &Value,
    key: &str,
    label: &str,
    maximum: usize,
) -> Result<String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .filter(|s| !s.trim().is_empty() && s.chars().count() <= maximum)
        .map(|s| s.trim().to_owned())
        .ok_or_else(|| {
            invalid(format!(
                "{label}: требуется непустая строка до {maximum} символов"
            ))
        })
}
pub(crate) fn s<'a>(value: &'a Value, key: &str) -> Result<&'a str> {
    value
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| invalid(format!("Отсутствует строка {key}")))
}
pub(crate) fn integer(value: &Value, key: &str) -> Result<u64> {
    value
        .get(key)
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid(format!("{key} должно быть целым числом")))
}
pub(crate) fn number(value: &Value, key: &str) -> Result<f64> {
    value
        .get(key)
        .and_then(Value::as_f64)
        .filter(|x| x.is_finite())
        .ok_or_else(|| invalid(format!("{key} должно быть конечным числом")))
}
pub(crate) fn decode<T: DeserializeOwned + RestoreKeys>(value: &Value) -> Result<T> {
    let bytes = ori_contracts::to_json_vec(value).map_err(|e| invalid(e.to_string()))?;
    ori_contracts::from_json_slice(&bytes, ori_monitoring::MAX_JSON_BYTES)
        .map_err(|e| invalid(e.to_string()))
}
pub(crate) fn value<T: Serialize>(value: &T) -> Result<Value> {
    let bytes = ori_contracts::to_json_vec(value).map_err(|e| invalid(e.to_string()))?;
    ori_contracts::parse_json_value(&bytes, ori_monitoring::MAX_JSON_BYTES)
        .map_err(|e| invalid(e.to_string()))
}
pub(crate) fn timestamp(raw: &str) -> Result<UtcTimestamp> {
    raw.parse()
        .map_err(|e: ori_contracts::CodecError| invalid(e.to_string()))
}
/// Python timedelta.total_seconds performs one correctly rounded integer ratio.
pub(crate) fn seconds(micros: i64) -> f64 {
    if micros == 0 {
        return 0.;
    }
    let n = micros.unsigned_abs() as u128;
    let d = 1_000_000u128;
    let mut exp = (128 - n.leading_zeros()) as i32 - (128 - d.leading_zeros()) as i32;
    if if exp >= 0 {
        n < d << exp
    } else {
        n << -exp < d
    } {
        exp -= 1;
    }
    let scaled = n << (52 - exp);
    let mut sig = scaled / d;
    let rem = scaled % d;
    if rem > d - rem || rem == d - rem && sig % 2 == 1 {
        sig += 1;
    }
    if sig == 1u128 << 53 {
        sig >>= 1;
        exp += 1;
    }
    let answer = f64::from_bits((((exp + 1023) as u64) << 52) | (sig as u64 - (1u64 << 52)));
    if micros < 0 { -answer } else { answer }
}
pub(crate) fn elapsed(start: &str, end: &str) -> Result<f64> {
    Ok(seconds(
        timestamp(end)?.unix_micros() - timestamp(start)?.unix_micros(),
    ))
}
pub(crate) fn median(mut values: Vec<f64>) -> Option<f64> {
    if values.is_empty() {
        return None;
    }
    values.sort_by(f64::total_cmp);
    let m = values.len() / 2;
    Some(if values.len().is_multiple_of(2) {
        (values[m - 1] + values[m]) / 2.
    } else {
        values[m]
    })
}
pub(crate) fn truncate(value: &str, maximum: usize) -> String {
    value.chars().take(maximum).collect()
}
pub(crate) fn py_str(value: &Value) -> String {
    match value {
        Value::Null => "None".into(),
        Value::Bool(v) => {
            if *v {
                "True".into()
            } else {
                "False".into()
            }
        }
        Value::String(s) => s.clone(),
        Value::Number(_) => ori_monitoring::python_canonical_json(value).unwrap_or_default(),
        _ => py_repr(value),
    }
}
fn py_repr(value: &Value) -> String {
    match value {
        Value::String(s) => {
            let q = if s.contains('\'') && !s.contains('"') {
                '"'
            } else {
                '\''
            };
            let mut out = String::from(q);
            for ch in s.chars() {
                match ch {
                    '\\' => out.push_str("\\\\"),
                    '\n' => out.push_str("\\n"),
                    '\r' => out.push_str("\\r"),
                    '\t' => out.push_str("\\t"),
                    ch if ch == q => {
                        out.push('\\');
                        out.push(ch);
                    }
                    ch if ch.is_control() => out.push_str(&format!("\\x{:02x}", ch as u32)),
                    ch => out.push(ch),
                }
            }
            out.push(q);
            out
        }
        Value::Array(a) => format!("[{}]", a.iter().map(py_repr).collect::<Vec<_>>().join(", ")),
        Value::Object(m) => format!(
            "{{{}}}",
            m.iter()
                .map(|(k, v)| format!("{}: {}", py_repr(&Value::String(k.clone())), py_repr(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
        _ => py_str(value),
    }
}
/// Same scalar formatting and spacing as Python JSON, with explicit UTF-8 mode.
pub(crate) fn spaced_json(value: &Value, ascii: bool) -> Result<String> {
    if ascii {
        return Ok(ori_monitoring::python_canonical_json(value)?);
    }
    Ok(match value {
        Value::String(s) => serde_json::to_string(s).map_err(|e| invalid(e.to_string()))?,
        Value::Array(a) => format!(
            "[{}]",
            a.iter()
                .map(|v| spaced_json(v, false))
                .collect::<Result<Vec<_>>>()?
                .join(", ")
        ),
        Value::Object(m) => format!(
            "{{{}}}",
            m.iter()
                .map(|(k, v)| Ok(format!(
                    "{}: {}",
                    serde_json::to_string(k).map_err(|e| invalid(e.to_string()))?,
                    spaced_json(v, false)?
                )))
                .collect::<Result<Vec<_>>>()?
                .join(", ")
        ),
        _ => ori_monitoring::python_canonical_json(value)?,
    })
}

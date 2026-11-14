use crate::PolicyKind;
use ori_session::{SessionError, SessionResult};
use serde_json::{Value, json};
use unicode_general_category::{GeneralCategory, get_general_category};

fn python_float(value: &str) -> Option<f64> {
    let decimal = value
        .trim()
        .chars()
        .map(|c| {
            if c.is_ascii_digit() || get_general_category(c) != GeneralCategory::DecimalNumber {
                return c;
            }
            let mut first = c as u32;
            while first > 0
                && char::from_u32(first - 1)
                    .is_some_and(|c| get_general_category(c) == GeneralCategory::DecimalNumber)
            {
                first -= 1;
            }
            char::from_digit((c as u32 - first) % 10, 10).expect("decimal digit")
        })
        .collect::<String>();
    let chars = decimal.chars().collect::<Vec<_>>();
    if chars.iter().enumerate().any(|(index, c)| {
        *c == '_'
            && (index == 0
                || index + 1 == chars.len()
                || !chars[index - 1].is_ascii_digit()
                || !chars[index + 1].is_ascii_digit())
    }) {
        return None;
    }
    let stripped = decimal.replace('_', "");
    match stripped.to_ascii_lowercase().as_str() {
        "inf" | "+inf" | "infinity" | "+infinity" => Some(f64::INFINITY),
        "-inf" | "-infinity" => Some(f64::NEG_INFINITY),
        "nan" | "+nan" | "-nan" => Some(f64::NAN),
        _ => stripped.parse().ok(),
    }
}

pub(crate) fn defaults(kind: PolicyKind) -> Value {
    match kind {
        PolicyKind::Detection => {
            json!({"enabled":false,"interval_seconds":1.0,"stream_id":null,"threshold_dbm":-70.0,"dedup_bandwidth_hz":100000,"cooldown_seconds":60.0,"max_candidates_per_sweep":10})
        }
        PolicyKind::Handoff => {
            json!({"enabled":false,"interval_seconds":1.0,"minimum_priority":0,"receiver_stream_id":null,"max_consecutive_failures":3,"retry_delay_seconds":30.0})
        }
        PolicyKind::Completion => {
            json!({"enabled":false,"interval_seconds":1.0,"minimum_listening_seconds":0.0,"minimum_observations":1,"receiver_stream_id":null})
        }
    }
}
fn number(value: &Value, name: &str, min: f64, max: f64) -> SessionResult<Value> {
    let result = match value {
        Value::Number(value) => value.as_f64(),
        Value::String(value) => python_float(value),
        _ => None,
    }
    .ok_or_else(|| SessionError::invalid(format!("{name} должно быть конечным числом")))?;
    if !result.is_finite() || result < min || result > max {
        return Err(SessionError::invalid(format!(
            "{name} должно быть в диапазоне {min:?}…{max:?}"
        )));
    }
    Ok(json!(result))
}
fn integer(value: &Value, name: &str, min: u64, max: u64) -> SessionResult<Value> {
    // serde keeps integer and float tokens distinct, as does Python's isinstance.
    let number = value
        .as_number()
        .filter(|number| !number.is_f64())
        .ok_or_else(|| SessionError::invalid(format!("{name} должно быть целым числом")))?;
    let result = number.as_u64();
    if result.is_none_or(|result| result < min || result > max) {
        return Err(SessionError::invalid(format!(
            "{name} должно быть в диапазоне {min}…{max}"
        )));
    }
    Ok(json!(result.unwrap()))
}
pub(crate) fn patch(kind: PolicyKind, payload: Value, session: &Value) -> SessionResult<Value> {
    let fields = payload
        .as_object()
        .ok_or_else(|| SessionError::invalid("Политика должна быть JSON-объектом"))?;
    let allowed = defaults(kind);
    let unknown = fields
        .keys()
        .filter(|k| allowed.get(k.as_str()).is_none())
        .cloned()
        .collect::<Vec<_>>();
    if !unknown.is_empty() {
        return Err(SessionError::invalid(format!(
            "Неизвестные поля политики: {}",
            unknown.join(", ")
        )));
    }
    let mut output = serde_json::Map::new();
    for (name, value) in fields {
        let result = match name.as_str() {
            "enabled" => Value::Bool(
                value
                    .as_bool()
                    .ok_or_else(|| SessionError::invalid("enabled должно быть JSON boolean"))?,
            ),
            "interval_seconds" => number(value, name, 0.1, 3600.0)?,
            "threshold_dbm" => number(value, name, -300.0, 100.0)?,
            "cooldown_seconds" | "minimum_listening_seconds" => number(value, name, 0.0, 86400.0)?,
            "retry_delay_seconds" => number(value, name, 0.1, 86400.0)?,
            "dedup_bandwidth_hz" => integer(value, name, 1, 10000000000)?,
            "max_candidates_per_sweep" | "max_consecutive_failures" => {
                integer(value, name, 1, 100)?
            }
            "minimum_priority" => integer(value, name, 0, 1000000)?,
            "minimum_observations" => integer(value, name, 1, 1000)?,
            "stream_id" | "receiver_stream_id" => {
                if !value.is_null() && value.as_str().is_none_or(|v| v.trim().is_empty()) {
                    return Err(SessionError::invalid(format!(
                        "{name} должно быть непустой строкой или null"
                    )));
                }
                if let Some(id) = value.as_str() {
                    let stream = session["streams"]
                        .as_array()
                        .and_then(|items| items.iter().find(|v| v["id"] == id))
                        .ok_or_else(|| {
                            SessionError::invalid(format!("{name} не принадлежит сеансу"))
                        })?;
                    let role = if kind == PolicyKind::Detection {
                        "spectrum_analyzer"
                    } else {
                        "receiver"
                    };
                    if stream["role"] != role {
                        return Err(SessionError::invalid(format!(
                            "Политика может выбирать только {role}"
                        )));
                    }
                    if kind == PolicyKind::Detection && stream["level_unit"] != "dBm" {
                        return Err(SessionError::invalid(
                            "Политика может выбирать только поток с исходными dBm",
                        ));
                    }
                }
                value.clone()
            }
            _ => unreachable!("validated policy field"),
        };
        output.insert(name.clone(), result);
    }
    Ok(Value::Object(output))
}
pub(crate) fn validate_shape(kind: PolicyKind, payload: &Value) -> SessionResult<()> {
    let fields = payload
        .as_object()
        .ok_or_else(|| SessionError::invalid("Политика должна быть JSON-объектом"))?;
    let allowed = defaults(kind);
    let unknown = fields
        .keys()
        .filter(|k| allowed.get(k.as_str()).is_none())
        .cloned()
        .collect::<Vec<_>>();
    if !unknown.is_empty() {
        return Err(SessionError::invalid(format!(
            "Неизвестные поля политики: {}",
            unknown.join(", ")
        )));
    }
    Ok(())
}

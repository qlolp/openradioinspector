use crate::{SessionError, SessionResult};
use chrono::{DateTime, Datelike, NaiveDate, NaiveDateTime, SecondsFormat, Utc};
use serde_json::{Value, json};
use unicode_general_category::{GeneralCategory, get_general_category};
use unicode_normalization::UnicodeNormalization;

pub(crate) fn field<'a>(value: &'a Value, name: &str) -> SessionResult<&'a Value> {
    value.get(name).ok_or_else(|| SessionError::field(name))
}
pub(crate) fn text(value: &Value) -> &str {
    value.as_str().unwrap_or("")
}
pub(crate) fn py_string(value: &Value) -> String {
    match value {
        Value::String(v) => v.clone(),
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        _ => value.to_string(),
    }
}
pub(crate) fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(v) => *v,
        Value::String(v) => !v.is_empty(),
        Value::Array(v) => !v.is_empty(),
        Value::Object(v) => !v.is_empty(),
        Value::Number(v) => v.as_f64() != Some(0.0),
    }
}
pub(crate) fn required(value: &str, name: &str) -> SessionResult<String> {
    if value.trim().is_empty() {
        return Err(SessionError::invalid(format!(
            "{name} не должно быть пустым"
        )));
    }
    Ok(value.trim().into())
}
pub(crate) fn integer(value: &Value) -> bool {
    value
        .as_number()
        .is_some_and(|v| !v.to_string().bytes().any(|b| b".eE".contains(&b)))
}
pub(crate) fn frequency(value: &Value, name: &str) -> SessionResult<u64> {
    if !integer(value) {
        return Err(SessionError::invalid(format!(
            "{name} должно быть целым числом Гц"
        )));
    }
    if value.to_string().starts_with('-') {
        return Err(SessionError::invalid(format!(
            "{name} не может быть отрицательным"
        )));
    }
    let value = value
        .as_u64()
        .filter(|v| *v <= i64::MAX as u64)
        .ok_or_else(|| SessionError::invalid(format!("{name} вне диапазона целых Hz")))?;
    if value == 0 {
        return Err(SessionError::invalid(format!(
            "{name} должно быть положительным"
        )));
    }
    Ok(value)
}
pub(crate) fn finite(value: &Value, name: &str) -> SessionResult<Value> {
    if value.is_null() {
        return Ok(Value::Null);
    }
    let number = match value {
        Value::Bool(v) => u8::from(*v) as f64,
        Value::String(original) => {
            let decimal: String = original
                .chars()
                .map(|c| {
                    if c.is_ascii() || get_general_category(c) != GeneralCategory::DecimalNumber {
                        return c;
                    }
                    // Unicode decimal digits occur in consecutive 0..9 blocks;
                    // adjacent mathematical styles form several such blocks.
                    let mut first = c as u32;
                    while first > 0
                        && char::from_u32(first - 1).is_some_and(|c| {
                            get_general_category(c) == GeneralCategory::DecimalNumber
                        })
                    {
                        first -= 1;
                    }
                    char::from_digit((c as u32 - first) % 10, 10).expect("decimal digit")
                })
                .collect();
            let chars = decimal.chars().collect::<Vec<_>>();
            let valid = chars.iter().enumerate().all(|(index, c)| {
                *c != '_'
                    || (index > 0
                        && index + 1 < chars.len()
                        && chars[index - 1].is_ascii_digit()
                        && chars[index + 1].is_ascii_digit())
            });
            let stripped = decimal.replace('_', "");
            if !valid {
                return Err(SessionError::invalid(format!(
                    "could not convert string to float: {}",
                    py_repr(original)
                )));
            }
            let input = stripped.trim();
            match input.to_ascii_lowercase().as_str() {
                "inf" | "+inf" | "infinity" | "+infinity" => f64::INFINITY,
                "-inf" | "-infinity" => f64::NEG_INFINITY,
                "nan" | "+nan" | "-nan" => f64::NAN,
                _ => input.parse::<f64>().map_err(|_| {
                    SessionError::invalid(format!(
                        "could not convert string to float: {}",
                        py_repr(original)
                    ))
                })?,
            }
        }
        Value::Array(_) => {
            return Err(SessionError::invalid(
                "float() argument must be a string or a real number, not 'list'",
            ));
        }
        Value::Object(_) => {
            return Err(SessionError::invalid(
                "float() argument must be a string or a real number, not 'dict'",
            ));
        }
        _ => {
            let number = value.as_f64().unwrap_or(f64::INFINITY);
            if integer(value) && !number.is_finite() {
                return Err(SessionError::invalid("int too large to convert to float"));
            }
            number
        }
    };
    if !number.is_finite() {
        return Err(SessionError::invalid(format!(
            "{name} должно быть конечным числом"
        )));
    }
    Ok(json!(number))
}
fn py_repr(value: &str) -> String {
    let quote = if value.contains('\'') && !value.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut output = String::new();
    output.push(quote);
    for c in value.chars() {
        match c {
            '\\' => output.push_str("\\\\"),
            '\n' => output.push_str("\\n"),
            '\r' => output.push_str("\\r"),
            '\t' => output.push_str("\\t"),
            c if c == quote => {
                output.push('\\');
                output.push(c);
            }
            c if matches!(
                get_general_category(c),
                GeneralCategory::Control
                    | GeneralCategory::Format
                    | GeneralCategory::PrivateUse
                    | GeneralCategory::Surrogate
                    | GeneralCategory::Unassigned
                    | GeneralCategory::LineSeparator
                    | GeneralCategory::ParagraphSeparator
            ) || (get_general_category(c) == GeneralCategory::SpaceSeparator && c != ' ') =>
            {
                use std::fmt::Write;
                if c as u32 <= 0xff {
                    write!(&mut output, "\\x{:02x}", c as u32).expect("String");
                } else if c as u32 <= 0xffff {
                    write!(&mut output, "\\u{:04x}", c as u32).expect("String");
                } else {
                    write!(&mut output, "\\U{:08x}", c as u32).expect("String");
                }
            }
            c => output.push(c),
        }
    }
    output.push(quote);
    output
}
pub(crate) fn stamp(value: DateTime<Utc>) -> String {
    value.to_rfc3339_opts(
        if value.timestamp_subsec_micros() == 0 {
            SecondsFormat::Secs
        } else {
            SecondsFormat::Micros
        },
        true,
    )
}
pub(crate) fn parse_time(value: &Value, name: &str) -> SessionResult<DateTime<Utc>> {
    let Some(text) = value.as_str().filter(|v| !v.trim().is_empty()) else {
        return Err(SessionError::invalid(format!(
            "{name} должно быть ISO-строкой"
        )));
    };
    let mut canonical = text.to_owned();
    if canonical.as_bytes().get(10) == Some(&b' ') {
        canonical.replace_range(10..11, "T");
    }
    match DateTime::parse_from_rfc3339(&canonical) {
        Ok(time) if (1..=9999).contains(&time.year()) => {
            DateTime::from_timestamp_micros(time.timestamp_micros())
                .ok_or_else(|| SessionError::invalid(format!("Некорректное {name}: {text}")))
        }
        _ => {
            let naive = NaiveDateTime::parse_from_str(&canonical, "%Y-%m-%dT%H:%M:%S%.f").is_ok()
                || NaiveDateTime::parse_from_str(&canonical, "%Y-%m-%dT%H:%M").is_ok()
                || NaiveDate::parse_from_str(&canonical, "%Y-%m-%d").is_ok();
            Err(SessionError::invalid(if naive {
                format!("{name} должно содержать часовой пояс")
            } else {
                format!("Некорректное {name}: {text}")
            }))
        }
    }
}
pub(crate) fn interval(value: &Value) -> SessionResult<Value> {
    let start = parse_time(field(value, "started_at")?, "started_at")?;
    let end = parse_time(field(value, "ended_at")?, "ended_at")?;
    if end < start {
        return Err(SessionError::invalid(
            "ended_at не может предшествовать started_at",
        ));
    }
    Ok(json!({"started_at":stamp(start),"ended_at":stamp(end)}))
}
pub(crate) fn exact_keys(value: &Value, keys: &[&str]) -> bool {
    value
        .as_object()
        .is_some_and(|v| v.len() == keys.len() && keys.iter().all(|key| v.contains_key(*key)))
}
pub(crate) fn name(value: &Value) -> SessionResult<String> {
    let Some(value) = value.as_str() else {
        return Err(SessionError::invalid("Имя эталона должно быть строкой"));
    };
    let value: String = value.nfc().collect();
    if value.chars().any(|c| {
        matches!(
            get_general_category(c),
            GeneralCategory::Control
                | GeneralCategory::Format
                | GeneralCategory::PrivateUse
                | GeneralCategory::Surrogate
                | GeneralCategory::Unassigned
        )
    }) {
        return Err(SessionError::invalid(
            "Имя эталона не должно содержать управляющие символы Unicode",
        ));
    }
    let value = value.trim();
    if !(1..=80).contains(&value.chars().count()) {
        return Err(SessionError::invalid(
            "Имя эталона должно содержать 1–80 символов",
        ));
    }
    Ok(value.into())
}

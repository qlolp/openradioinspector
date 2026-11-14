//! Existing Python query-number grammar; no raw-source coercion is performed.
use ori_legacy::{LegacyError, Result};
use unicode_general_category::{GeneralCategory, get_general_category};

fn decimal(value: &str) -> String {
    value
        .trim()
        .chars()
        .map(|character| {
            if character.is_ascii_digit()
                || get_general_category(character) != GeneralCategory::DecimalNumber
            {
                return character;
            }
            let mut first = character as u32;
            while first > 0
                && char::from_u32(first - 1).is_some_and(|character| {
                    get_general_category(character) == GeneralCategory::DecimalNumber
                })
            {
                first -= 1;
            }
            char::from_digit((character as u32 - first) % 10, 10).expect("decimal digit")
        })
        .collect()
}
fn unseparated(value: &str) -> Option<String> {
    let bytes = value.as_bytes();
    if bytes.iter().enumerate().any(|(index, byte)| {
        *byte == b'_'
            && (index == 0
                || index + 1 == bytes.len()
                || !bytes[index - 1].is_ascii_digit()
                || !bytes[index + 1].is_ascii_digit())
    }) {
        None
    } else {
        Some(value.replace('_', ""))
    }
}
fn quoted(value: &str) -> String {
    let quote = if value.contains('\'') && !value.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::new();
    out.push(quote);
    for character in value.chars() {
        match character {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if c.is_control() => {
                let code = c as u32;
                if code <= 0xff {
                    out.push_str(&format!("\\x{code:02x}"));
                } else {
                    out.push_str(&format!("\\u{code:04x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}
pub(super) fn float(raw: &str) -> Result<f64> {
    let value = decimal(raw);
    let value = unseparated(&value)
        .and_then(|value| match value.to_ascii_lowercase().as_str() {
            "inf" | "+inf" | "infinity" | "+infinity" => Some(f64::INFINITY),
            "-inf" | "-infinity" => Some(f64::NEG_INFINITY),
            "nan" | "+nan" | "-nan" => Some(f64::NAN),
            _ => value.parse().ok(),
        })
        .ok_or_else(|| {
            LegacyError::Request(format!(
                "could not convert string to float: {}",
                quoted(raw)
            ))
        })?;
    if !value.is_finite() {
        return Err(LegacyError::Request("Число должно быть конечным".into()));
    }
    Ok(value)
}
pub(super) fn dimension(raw: Option<&String>) -> Result<u32> {
    let Some(raw) = raw else { return Ok(30) };
    let converted = decimal(raw);
    let normalized = unseparated(&converted).ok_or_else(|| {
        LegacyError::Request(format!(
            "invalid literal for int() with base 10: {}",
            quoted(raw)
        ))
    })?;
    let magnitude = normalized.strip_prefix(['+', '-']).unwrap_or(&normalized);
    if magnitude.is_empty() || !magnitude.bytes().all(|value| value.is_ascii_digit()) {
        return Err(LegacyError::Request(format!(
            "invalid literal for int() with base 10: {}",
            quoted(raw)
        )));
    }
    let value = normalized
        .parse::<i64>()
        .ok()
        .filter(|value| (1..=100).contains(value));
    value
        .map(|value| value as u32)
        .ok_or_else(|| LegacyError::Request("Размеры сетки должны быть от 1 до 100".into()))
}

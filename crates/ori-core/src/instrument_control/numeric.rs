//! Existing Python int(query) grammar, applied only to artifact pagination.
//! Source IDs, measurements, settings and raw metadata are never coerced here.
use crate::error::{ApiError, ApiResult};
use axum::http::StatusCode;
use unicode_general_category::{GeneralCategory, get_general_category};

pub(super) fn count(raw: &str, key: &str, min: usize, max: usize) -> ApiResult<usize> {
    let invalid = || {
        ApiError::new(
            StatusCode::BAD_REQUEST,
            "instrument_invalid",
            format!(
                "invalid literal for int() with base 10: {}",
                super::http::quoted(raw)
            ),
        )
    };
    let bound = || {
        ApiError::new(
            StatusCode::BAD_REQUEST,
            "instrument_invalid",
            format!("Artifact {key} must be {min}..{max}"),
        )
    };
    let converted = raw
        .trim_matches(char::is_whitespace)
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
    let bytes = converted.as_bytes();
    if bytes.iter().enumerate().any(|(i, b)| {
        *b == b'_'
            && (i == 0
                || i + 1 == bytes.len()
                || !bytes[i - 1].is_ascii_digit()
                || !bytes[i + 1].is_ascii_digit())
    }) {
        return Err(invalid());
    }
    let normalized = converted.replace('_', "");
    let negative = normalized.starts_with('-');
    let magnitude = normalized.strip_prefix(['+', '-']).unwrap_or(&normalized);
    if magnitude.is_empty() || !magnitude.bytes().all(|b| b.is_ascii_digit()) {
        return Err(invalid());
    }
    if magnitude.len() > 4300 {
        return Err(ApiError::new(
            StatusCode::BAD_REQUEST,
            "instrument_invalid",
            format!(
                "Exceeds the limit (4300 digits) for integer string conversion: value has {} digits; use sys.set_int_max_str_digits() to increase the limit",
                magnitude.len()
            ),
        ));
    }
    let digits = magnitude.trim_start_matches('0');
    let value = if digits.is_empty() {
        0
    } else {
        if negative || digits.len() > max.to_string().len() {
            return Err(bound());
        }
        digits.parse::<usize>().map_err(|_| bound())?
    };
    if !(min..=max).contains(&value) {
        return Err(bound());
    }
    Ok(value)
}

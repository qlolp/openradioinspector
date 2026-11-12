//! Match the uploaded NumPy float64 -> clipped/truncated little-endian PCM rule.
use crate::error::{ApiError, ApiResult};
use serde_json::Value;
use unicode_general_category::{GeneralCategory, get_general_category};

pub(super) const MAX_PUSH_FRAMES: usize = 480_000;
pub(super) const MAX_LIVE_FRAMES: usize = 48_000;

pub(super) fn decode_samples(value: &Value) -> ApiResult<Vec<i16>> {
    let shape = shape(value).map_err(|prefix| {
        let dimensions = prefix.len();
        let tuple = if dimensions == 1 { format!("{},", prefix[0]) }
            else { prefix.iter().map(usize::to_string).collect::<Vec<_>>().join(", ") };
        ApiError::invalid(format!("setting an array element with a sequence. The requested array has an inhomogeneous shape after {dimensions} dimensions. The detected shape was ({tuple}) + inhomogeneous part."))
    })?;
    if shape.len() > 64 {
        return Err(ApiError::invalid(
            "setting an array element with a sequence. The requested array would exceed the maximum number of dimension of 64.",
        ));
    }
    let count = shape
        .iter()
        .try_fold(1usize, |n, dimension| n.checked_mul(*dimension))
        .filter(|n| *n <= MAX_PUSH_FRAMES)
        .ok_or_else(|| {
            super::phonogram(format!("Один вызов не более {MAX_PUSH_FRAMES} отсчётов"))
        })?;
    let mut result = Vec::new();
    result
        .try_reserve_exact(count)
        .map_err(|_| super::capacity())?;
    let mut finite = true;
    flatten(value, &mut result, &mut finite)?;
    if !finite {
        return Err(super::phonogram("Отсчёты должны быть конечными числами"));
    }
    Ok(result)
}

// The common shape prefix is NumPy's reported inhomogeneous dimension. JSON
// nesting is already bounded by the request parser, not an unbounded iterator.
fn shape(value: &Value) -> Result<Vec<usize>, Vec<usize>> {
    let Some(items) = value.as_array() else {
        return Ok(Vec::new());
    };
    let mut result = vec![items.len()];
    if items.is_empty() {
        return Ok(result);
    }
    let (mut common, mut ragged) = match shape(&items[0]) {
        Ok(shape) => (shape, false),
        Err(prefix) => (prefix, true),
    };
    for item in &items[1..] {
        let (next, invalid) = match shape(item) {
            Ok(shape) => (shape, false),
            Err(prefix) => (prefix, true),
        };
        ragged |= invalid;
        let matching = common.iter().zip(&next).take_while(|(a, b)| a == b).count();
        if matching != common.len() || matching != next.len() {
            ragged = true;
            common.truncate(matching);
        }
    }
    result.extend(common);
    if ragged { Err(result) } else { Ok(result) }
}
fn flatten(value: &Value, output: &mut Vec<i16>, finite: &mut bool) -> ApiResult<()> {
    if let Some(items) = value.as_array() {
        for item in items {
            flatten(item, output, finite)?;
        }
        return Ok(());
    }
    let number = match value {
        Value::Null => f64::NAN,
        Value::Bool(flag) => {
            if *flag {
                1.0
            } else {
                0.0
            }
        }
        Value::Number(number) => number
            .as_f64()
            .filter(|n| n.is_finite())
            .ok_or_else(|| ApiError::invalid("int too large to convert to float"))?,
        Value::String(text) => parse_float(text).ok_or_else(|| {
            ApiError::invalid(format!(
                "could not convert string to float: {}",
                python_repr(text)
            ))
        })?,
        Value::Object(_) => {
            return Err(ApiError::invalid(
                "float() argument must be a string or a real number, not 'dict'",
            ));
        }
        Value::Array(_) => unreachable!(),
    };
    *finite &= number.is_finite();
    output.push((number.clamp(-1.0, 1.0) * 32767.0) as i16);
    Ok(())
}
fn parse_float(text: &str) -> Option<f64> {
    let normalized;
    let text = if text.is_ascii() {
        text.trim()
    } else {
        normalized = text
            .trim()
            .chars()
            .map(|ch| {
                if !ch.is_ascii() && get_general_category(ch) == GeneralCategory::DecimalNumber {
                    // Unicode Nd consists of consecutive 0..9 sets, including
                    // adjacent mathematical styles. No locale or I/O is involved.
                    let mut first = ch as u32;
                    while first > 0
                        && char::from_u32(first - 1).is_some_and(|previous| {
                            get_general_category(previous) == GeneralCategory::DecimalNumber
                        })
                    {
                        first -= 1;
                    }
                    char::from(b'0' + ((ch as u32 - first) % 10) as u8)
                } else {
                    ch
                }
            })
            .collect::<String>();
        &normalized
    };
    // Python's float grammar permits underscores only between decimal digits.
    if text.contains('_') {
        let chars = text.chars().collect::<Vec<_>>();
        if chars.iter().enumerate().any(|(i, ch)| {
            *ch == '_'
                && (i == 0
                    || i + 1 == chars.len()
                    || !chars[i - 1].is_ascii_digit()
                    || !chars[i + 1].is_ascii_digit())
        }) {
            return None;
        }
        return ascii_float(
            &chars
                .into_iter()
                .filter(|ch| *ch != '_')
                .collect::<String>(),
        );
    }
    ascii_float(text)
}
fn ascii_float(text: &str) -> Option<f64> {
    match text.to_ascii_lowercase().as_str() {
        "inf" | "+inf" | "infinity" | "+infinity" => Some(f64::INFINITY),
        "-inf" | "-infinity" => Some(f64::NEG_INFINITY),
        "nan" | "+nan" | "-nan" => Some(f64::NAN),
        _ => text.parse().ok(),
    }
}
pub(super) fn python_repr(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut repr = String::new();
    repr.push(quote);
    for ch in text.chars() {
        match ch {
            '\\' => repr.push_str("\\\\"),
            '\n' => repr.push_str("\\n"),
            '\r' => repr.push_str("\\r"),
            '\t' => repr.push_str("\\t"),
            ch if ch == quote => {
                repr.push('\\');
                repr.push(ch);
            }
            ch if ch != ' '
                && matches!(
                    get_general_category(ch),
                    GeneralCategory::Control
                        | GeneralCategory::Format
                        | GeneralCategory::Unassigned
                        | GeneralCategory::PrivateUse
                        | GeneralCategory::SpaceSeparator
                        | GeneralCategory::LineSeparator
                        | GeneralCategory::ParagraphSeparator
                ) =>
            {
                let cp = ch as u32;
                if cp <= 0xff {
                    repr.push_str(&format!("\\x{cp:02x}"));
                } else if cp <= 0xffff {
                    repr.push_str(&format!("\\u{cp:04x}"));
                } else {
                    repr.push_str(&format!("\\U{cp:08x}"));
                }
            }
            ch => repr.push(ch),
        }
    }
    repr.push(quote);
    repr
}

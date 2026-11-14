use crate::{TaskError, TaskResult};
use chrono::{
    DateTime, Datelike, NaiveDate, NaiveDateTime, SecondsFormat, TimeDelta, Timelike, Utc,
};
use serde_json::{Value, json};

#[derive(Clone)]
pub(crate) struct Moment {
    local: NaiveDateTime,
    offset_micros: Option<i64>,
}
fn format(micros: u32) -> SecondsFormat {
    if micros == 0 {
        SecondsFormat::Secs
    } else {
        SecondsFormat::Micros
    }
}
pub(crate) fn utc(value: DateTime<Utc>) -> String {
    value.to_rfc3339_opts(format(value.timestamp_subsec_micros()), true)
}
fn decimal(value: &str) -> Option<u32> {
    if value.is_empty() || !value.bytes().all(|c| c.is_ascii_digit()) {
        None
    } else {
        value.parse().ok()
    }
}
fn hms(value: &str) -> Option<(u32, u32, u32, u32)> {
    let (clock, fraction) = match value.find(['.', ',']) {
        Some(index) => (&value[..index], Some(&value[index + 1..])),
        None => (value, None),
    };
    let (hours, minutes, seconds) = if clock.contains(':') {
        let parts = clock.split(':').collect::<Vec<_>>();
        if !(1..=3).contains(&parts.len()) || parts.iter().any(|v| v.len() != 2) {
            return None;
        }
        (
            decimal(parts[0])?,
            if parts.len() > 1 {
                decimal(parts[1])?
            } else {
                0
            },
            if parts.len() > 2 {
                decimal(parts[2])?
            } else {
                0
            },
        )
    } else {
        if ![2, 4, 6].contains(&clock.len()) || !clock.is_ascii() {
            return None;
        }
        (
            decimal(&clock[..2])?,
            if clock.len() > 2 {
                decimal(&clock[2..4])?
            } else {
                0
            },
            if clock.len() > 4 {
                decimal(&clock[4..6])?
            } else {
                0
            },
        )
    };
    let micros = if let Some(fraction) = fraction {
        if fraction.is_empty() || !fraction.bytes().all(|c| c.is_ascii_digit()) {
            return None;
        }
        let mut canonical = fraction[..fraction.len().min(6)].to_owned();
        while canonical.len() < 6 {
            canonical.push('0');
        }
        decimal(&canonical)?
    } else {
        0
    };
    Some((hours, minutes, seconds, micros))
}
fn date(value: &str) -> Option<NaiveDate> {
    let parsed = match value.len() {
        10 if value.as_bytes().get(4) == Some(&b'-') && value.as_bytes().get(5) == Some(&b'W') => {
            NaiveDate::parse_from_str(value, "%G-W%V-%u")
        }
        10 => NaiveDate::parse_from_str(value, "%Y-%m-%d"),
        8 if value.as_bytes().get(4) == Some(&b'W') => NaiveDate::parse_from_str(value, "%GW%V%u"),
        8 if value.as_bytes().get(4) == Some(&b'-') => {
            NaiveDate::parse_from_str(&format!("{value}-1"), "%G-W%V-%u")
        }
        8 => NaiveDate::parse_from_str(value, "%Y%m%d"),
        7 => NaiveDate::parse_from_str(&format!("{value}1"), "%GW%V%u"),
        _ => return None,
    }
    .ok()?;
    ((1..=9999).contains(&parsed.year())).then_some(parsed)
}
impl Moment {
    pub(crate) fn parse(text: &str) -> Option<Self> {
        for length in [10, 8, 7] {
            let Some(prefix) = text.get(..length) else {
                continue;
            };
            let Some(date) = date(prefix) else {
                continue;
            };
            if text.len() == length {
                return Some(Self {
                    local: date.and_hms_opt(0, 0, 0)?,
                    offset_micros: None,
                });
            }
            let suffix = &text[length..];
            let separator = suffix.chars().next()?;
            let time = &suffix[separator.len_utf8()..];
            if let Some(value) = Self::time(date, time) {
                return Some(value);
            }
        }
        None
    }
    fn time(date: NaiveDate, time: &str) -> Option<Self> {
        let zone = time.find(['+', '-', 'Z']);
        let clock = &time[..zone.unwrap_or(time.len())];
        let (h, m, s, micros) = hms(clock)?;
        let local = date.and_hms_micro_opt(h, m, s, micros)?;
        let offset_micros = if let Some(index) = zone {
            let zone = &time[index..];
            if zone == "Z" {
                Some(0)
            } else {
                if !zone.starts_with(['+', '-']) {
                    return None;
                }
                let (h, m, s, micros) = hms(&zone[1..])?;
                let seconds = i64::from(h) * 3600 + i64::from(m) * 60 + i64::from(s);
                // CPython treats an all-zero whole offset as UTC even when a
                // fractional offset was supplied; retain that existing rule.
                let total = if seconds == 0 {
                    0
                } else {
                    seconds * 1_000_000 + i64::from(micros)
                };
                if total >= 86_400_000_000 {
                    return None;
                }
                Some(if zone.starts_with('-') { -total } else { total })
            }
        } else {
            None
        };
        Some(Self {
            local,
            offset_micros,
        })
    }
    pub(crate) fn iso(&self) -> String {
        let mut result = self
            .local
            .format(if self.local.nanosecond() == 0 {
                "%Y-%m-%dT%H:%M:%S"
            } else {
                "%Y-%m-%dT%H:%M:%S%.6f"
            })
            .to_string();
        if let Some(offset) = self.offset_micros {
            let total = offset.unsigned_abs();
            let seconds = total / 1_000_000;
            let micros = total % 1_000_000;
            use std::fmt::Write;
            write!(
                &mut result,
                "{}{:02}:{:02}",
                if offset < 0 { '-' } else { '+' },
                seconds / 3600,
                (seconds / 60) % 60
            )
            .expect("String");
            if seconds % 60 != 0 || micros != 0 {
                write!(&mut result, ":{:02}", seconds % 60).expect("String");
                if micros != 0 {
                    write!(&mut result, ".{micros:06}").expect("String");
                }
            }
        }
        result
    }
    fn as_utc(&self) -> TaskResult<DateTime<Utc>> {
        let offset = self
            .offset_micros
            .ok_or_else(|| TaskError::invalid("время должно содержать часовой пояс"))?;
        self.local
            .checked_sub_signed(TimeDelta::microseconds(offset))
            .map(|v| v.and_utc())
            .ok_or_else(|| TaskError::invalid("date value out of range"))
    }
    pub(crate) fn aware(&self) -> TaskResult<DateTime<Utc>> {
        let value = self.as_utc()?;
        if !(1..=9999).contains(&value.year()) {
            return Err(TaskError::invalid("date value out of range"));
        }
        Ok(value)
    }
    pub(crate) fn due(&self, now: DateTime<Utc>) -> TaskResult<bool> {
        if self.offset_micros.is_none() {
            return Err(TaskError::invalid(
                "can't compare offset-naive and offset-aware datetimes",
            ));
        }
        Ok(now >= self.as_utc()?)
    }
    pub(crate) fn ends(&self, seconds: f64) -> TaskResult<Self> {
        let whole = seconds.trunc();
        let days = (whole / 86_400.).floor();
        if !seconds.is_finite() || days < i32::MIN as f64 || days > i32::MAX as f64 {
            return Err(TaskError::invalid(
                "Python int too large to convert to C int",
            ));
        }
        if days.abs() > 999_999_999. {
            return Err(TaskError::invalid(format!(
                "days={}; must have magnitude <= 999999999",
                days as i32
            )));
        }
        let fraction = (seconds.fract() * 1_000_000.).round_ties_even() as i64;
        let span = TimeDelta::try_seconds(whole as i64)
            .and_then(|v| v.checked_add(&TimeDelta::microseconds(fraction)))
            .ok_or_else(|| TaskError::invalid("date value out of range"))?;
        let local = self
            .local
            .checked_add_signed(span)
            .filter(|v| (1..=9999).contains(&v.year()))
            .ok_or_else(|| TaskError::invalid("date value out of range"))?;
        Ok(Self {
            local,
            offset_micros: self.offset_micros,
        })
    }
}
pub(crate) fn parse_api(text: &str) -> TaskResult<DateTime<Utc>> {
    if super::model::strip(text).is_empty() {
        return Err(TaskError::invalid("время должно быть ISO-строкой"));
    }
    Moment::parse(&text.replace('Z', "+00:00"))
        .ok_or_else(|| TaskError::invalid(format!("Некорректное время: {text}")))?
        .aware()
}
pub(crate) fn stored_moment(value: &Value, required: bool) -> TaskResult<Value> {
    if !super::model::truthy(value) {
        return Ok(Value::Null);
    }
    let raw = value.as_str().ok_or_else(TaskError::book_invalid)?;
    match Moment::parse(raw) {
        Some(v) => Ok(json!(v.iso())),
        None if !required => Ok(Value::Null),
        None => Err(TaskError::book_invalid()),
    }
}

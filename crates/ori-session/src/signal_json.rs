//! Signal-list JSON follows Python's strict hooks: constants fail immediately,
//! duplicate object fields fail when their object is complete, floats are f64.
use crate::{SessionError, SessionResult};
use serde_json::Value;
use std::collections::HashSet;

fn invalid() -> SessionError {
    SessionError::invalid("Некорректный JSON UTF-8")
}
pub(crate) fn parse(text: &str) -> SessionResult<Value> {
    let mut parser = Parser {
        bytes: text.as_bytes(),
        index: 0,
    };
    let value = parser.value(0)?;
    parser.space();
    if parser.index != parser.bytes.len() {
        return Err(invalid());
    }
    Ok(value)
}
struct Parser<'a> {
    bytes: &'a [u8],
    index: usize,
}
impl Parser<'_> {
    fn current(&self) -> Option<u8> {
        self.bytes.get(self.index).copied()
    }
    fn space(&mut self) {
        while self.current().is_some_and(|c| b" \t\r\n".contains(&c)) {
            self.index += 1;
        }
    }
    fn take(&mut self, c: u8) -> bool {
        if self.current() == Some(c) {
            self.index += 1;
            true
        } else {
            false
        }
    }
    fn string(&mut self) -> SessionResult<String> {
        let start = self.index;
        if !self.take(b'"') {
            return Err(invalid());
        }
        while let Some(c) = self.current() {
            self.index += 1;
            if c == b'\\' {
                self.index += 1;
            } else if c == b'"' {
                return serde_json::from_slice(&self.bytes[start..self.index])
                    .map_err(|_| invalid());
            }
        }
        Err(invalid())
    }
    fn value(&mut self, depth: usize) -> SessionResult<Value> {
        if depth > 128 {
            return Err(invalid());
        }
        self.space();
        if self.current() == Some(b'"') {
            return self.string().map(Value::String);
        }
        if self.take(b'{') {
            let mut pairs = Vec::new();
            self.space();
            if self.take(b'}') {
                return Ok(Value::Object(Default::default()));
            }
            loop {
                self.space();
                let key = self.string()?;
                self.space();
                if !self.take(b':') {
                    return Err(invalid());
                }
                pairs.push((key, self.value(depth + 1)?));
                self.space();
                if self.take(b'}') {
                    break;
                }
                if !self.take(b',') {
                    return Err(invalid());
                }
            }
            let mut keys = HashSet::new();
            let mut object = serde_json::Map::new();
            for (key, value) in pairs {
                if !keys.insert(key.clone()) {
                    return Err(SessionError::invalid("Поля JSON не должны повторяться"));
                }
                object.insert(key, value);
            }
            return Ok(Value::Object(object));
        }
        if self.take(b'[') {
            let mut output = Vec::new();
            self.space();
            if self.take(b']') {
                return Ok(Value::Array(output));
            }
            loop {
                output.push(self.value(depth + 1)?);
                self.space();
                if self.take(b']') {
                    break;
                }
                if !self.take(b',') {
                    return Err(invalid());
                }
            }
            return Ok(Value::Array(output));
        }
        let remaining = &self.bytes[self.index..];
        if [b"NaN".as_slice(), b"Infinity", b"-Infinity"]
            .iter()
            .any(|token| remaining.starts_with(token))
        {
            return Err(SessionError::invalid(
                "JSON не должен содержать NaN или Infinity",
            ));
        }
        for (token, value) in [
            (b"true".as_slice(), Value::Bool(true)),
            (b"false", Value::Bool(false)),
            (b"null", Value::Null),
        ] {
            if remaining.starts_with(token) {
                self.index += token.len();
                return Ok(value);
            }
        }
        let start = self.index;
        while self
            .current()
            .is_some_and(|c| b"0123456789-+.eE".contains(&c))
        {
            self.index += 1;
        }
        if self.index == start {
            return Err(invalid());
        }
        let raw = &self.bytes[start..self.index];
        let number = ori_archive::parse_json_value(raw, raw.len()).map_err(|error| {
            if error.to_string().contains("finite") {
                SessionError::invalid("JSON не должен содержать неконечные числа")
            } else {
                invalid()
            }
        })?;
        if !number.is_number() {
            return Err(invalid());
        }
        if raw.iter().any(|c| b".eE".contains(c)) {
            let value = number.as_f64().filter(|v| v.is_finite()).ok_or_else(|| {
                SessionError::invalid("JSON не должен содержать неконечные числа")
            })?;
            return Ok(serde_json::json!(value));
        }
        Ok(number)
    }
}

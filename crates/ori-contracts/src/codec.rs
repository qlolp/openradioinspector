//! Strict transport decoding and finite-only encoding. Semantic hardware policy
//! and mutation remain the core's responsibility.
use chrono::{DateTime, NaiveDate, NaiveDateTime, SecondsFormat, Utc};
use serde::{
    Deserialize, Deserializer, Serialize,
    de::{self, DeserializeOwned},
    ser,
};
use serde_json::Value;
use std::{fmt, str::FromStr};

#[derive(Debug, thiserror::Error)]
pub enum CodecError {
    #[error("JSON body exceeds {max_bytes} bytes")]
    TooLarge { max_bytes: usize },
    #[error("invalid JSON: {0}")]
    Json(#[from] serde_json::Error),
    #[error("invalid contract data: {0}")]
    Invalid(String),
}
impl ser::Error for CodecError {
    fn custom<T: fmt::Display>(message: T) -> Self {
        Self::Invalid(message.to_string())
    }
}

/// Six-digit UTC representation. Normalization uses integer datetime arithmetic.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct UtcTimestamp(String);
impl UtcTimestamp {
    pub fn as_str(&self) -> &str {
        &self.0
    }
    pub fn unix_micros(&self) -> i64 {
        DateTime::parse_from_rfc3339(&self.0)
            .expect("normalized timestamp")
            .timestamp_micros()
    }
}
impl fmt::Display for UtcTimestamp {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.fmt(f)
    }
}
impl FromStr for UtcTimestamp {
    type Err = CodecError;
    fn from_str(value: &str) -> Result<Self, Self::Err> {
        normalize_utc(value).map(Self)
    }
}
impl TryFrom<&str> for UtcTimestamp {
    type Error = CodecError;
    fn try_from(value: &str) -> Result<Self, Self::Error> {
        value.parse()
    }
}
impl TryFrom<String> for UtcTimestamp {
    type Error = CodecError;
    fn try_from(value: String) -> Result<Self, Self::Error> {
        value.parse()
    }
}
impl Serialize for UtcTimestamp {
    fn serialize<S: ser::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&self.0)
    }
}
impl<'de> Deserialize<'de> for UtcTimestamp {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        String::deserialize(deserializer)?
            .parse()
            .map_err(de::Error::custom)
    }
}

pub fn normalize_utc(value: &str) -> Result<String, CodecError> {
    let mut text = value.to_owned();
    if text.as_bytes().get(10) == Some(&b' ') {
        text.replace_range(10..11, "T");
    }
    if let Some(dot) = text.find('.') {
        let end = text[dot + 1..]
            .find(|c: char| !c.is_ascii_digit())
            .map(|i| dot + 1 + i)
            .unwrap_or(text.len());
        if end > dot + 7 {
            text.replace_range(dot + 7..end, "");
        }
    }
    let parsed = DateTime::parse_from_rfc3339(&text)
        .map(|date| date.with_timezone(&Utc))
        .or_else(|_| {
            let naive = NaiveDateTime::parse_from_str(&text, "%Y-%m-%dT%H:%M:%S%.f")
                .or_else(|_| NaiveDateTime::parse_from_str(&text, "%Y-%m-%dT%H:%M"))
                .or_else(|_| {
                    NaiveDate::parse_from_str(&text, "%Y-%m-%d")
                        .map(|d| d.and_hms_opt(0, 0, 0).unwrap())
                })?;
            Ok::<_, chrono::ParseError>(naive.and_utc())
        })
        .map_err(|error| CodecError::Invalid(format!("timestamp: {error}")))?;
    if chrono::Timelike::nanosecond(&parsed) >= 1_000_000_000 {
        return Err(CodecError::Invalid(
            "leap seconds are unsupported by the legacy core".into(),
        ));
    }
    if !(1..=9999).contains(&chrono::Datelike::year(&parsed)) {
        return Err(CodecError::Invalid(
            "UTC year outside four-digit range".into(),
        ));
    }
    Ok(parsed.to_rfc3339_opts(SecondsFormat::Micros, true))
}

pub fn deserialize_contract<'de, D: Deserializer<'de>>(
    deserializer: D,
) -> Result<String, D::Error> {
    let value = String::deserialize(deserializer)?;
    if value != crate::CONTRACT_NAME {
        return Err(de::Error::custom("unsupported contract"));
    }
    Ok(value)
}
pub fn deserialize_contract_version<'de, D: Deserializer<'de>>(
    deserializer: D,
) -> Result<u64, D::Error> {
    let value = u64::deserialize(deserializer)?;
    if value != crate::CONTRACT_VERSION {
        return Err(de::Error::custom("unsupported contract version"));
    }
    Ok(value)
}

pub fn deserialize_optional_contract_version<'de, D: Deserializer<'de>>(
    deserializer: D,
) -> Result<Option<u64>, D::Error> {
    let value = Option::<u64>::deserialize(deserializer)?;
    if value.is_some_and(|v| v != crate::CONTRACT_VERSION) {
        return Err(de::Error::custom("unsupported contract version"));
    }
    Ok(value)
}

/// Reject duplicate object keys (including escaped aliases) at every depth and
/// nonfinite/overflow numbers before deserializing the caller's model.
/// Restore transport-only key escapes after serde deserialization. Generated
/// models implement this; custom DTOs can restore their embedded Value fields.
pub trait RestoreKeys {
    fn restore_keys(&mut self);
}

const ESCAPE_PREFIX: &str = "$ori::escaped::";
fn escaped_key(key: String) -> String {
    if key.starts_with(ESCAPE_PREFIX)
        || matches!(
            key.as_str(),
            "$serde_json::private::Number" | "$serde_json::private::RawValue"
        )
    {
        format!("{ESCAPE_PREFIX}{key}")
    } else {
        key
    }
}
fn restored_key(key: String) -> String {
    key.strip_prefix(ESCAPE_PREFIX)
        .map(str::to_owned)
        .unwrap_or(key)
}
fn escape_keys(value: &mut Value) {
    match value {
        Value::Object(object) => {
            let old = std::mem::take(object);
            for (key, mut value) in old {
                escape_keys(&mut value);
                object.insert(escaped_key(key), value);
            }
        }
        Value::Array(array) => {
            for value in array {
                escape_keys(value);
            }
        }
        _ => {}
    }
}
impl RestoreKeys for Value {
    fn restore_keys(&mut self) {
        match self {
            Value::Object(object) => {
                let old = std::mem::take(object);
                for (key, mut value) in old {
                    value.restore_keys();
                    object.insert(restored_key(key), value);
                }
            }
            Value::Array(array) => {
                for value in array {
                    value.restore_keys();
                }
            }
            _ => {}
        }
    }
}
impl<T: RestoreKeys> RestoreKeys for std::collections::BTreeMap<String, T> {
    fn restore_keys(&mut self) {
        let old = std::mem::take(self);
        for (key, mut value) in old {
            value.restore_keys();
            self.insert(restored_key(key), value);
        }
    }
}
impl<T: RestoreKeys> RestoreKeys for Option<T> {
    fn restore_keys(&mut self) {
        if let Some(value) = self {
            value.restore_keys();
        }
    }
}
impl<T: RestoreKeys> RestoreKeys for Vec<T> {
    fn restore_keys(&mut self) {
        for value in self {
            value.restore_keys();
        }
    }
}
macro_rules! no_keys { ($($type:ty),*) => { $(impl RestoreKeys for $type { fn restore_keys(&mut self) {} })* }; }
no_keys!(String, bool, u64, i64, f64, UtcTimestamp);

/// Strict generic decoding with reversible collision-free key escaping.
/// Real legacy "$serde_json::private::*" metadata is never reinterpreted as
/// serde's arbitrary-precision transport protocol.
pub fn from_json_slice<T: DeserializeOwned + RestoreKeys>(
    bytes: &[u8],
    max_bytes: usize,
) -> Result<T, CodecError> {
    let mut raw = parse_json_value(bytes, max_bytes)?;
    escape_keys(&mut raw);
    // Use the JSON deserializer rather than ValueDeserializer: serde flatten
    // captures numbers through Content, whose visitor does not accept u128.
    // The JSON parser exposes arbitrary precision through its supported protocol.
    let escaped = serde_json::to_vec(&raw)?;
    let mut value: T = serde_json::from_slice(&escaped)?;
    value.restore_keys();
    Ok(value)
}

/// Parse archive/extension JSON without invoking serde_json's ValueVisitor.
/// Unknown keys and arbitrary integer tokens are retained byte-exactly.
pub fn parse_json_value(bytes: &[u8], max_bytes: usize) -> Result<Value, CodecError> {
    if bytes.len() > max_bytes {
        return Err(CodecError::TooLarge { max_bytes });
    }
    std::str::from_utf8(bytes).map_err(|_| CodecError::Invalid("invalid UTF-8".into()))?;
    let mut parser = JsonSyntax { bytes, index: 0 };
    let value = parser.value(0)?;
    parser.space();
    if parser.index != bytes.len() {
        return Err(CodecError::Invalid("trailing JSON data".into()));
    }
    Ok(value)
}
fn validate_json(bytes: &[u8]) -> Result<(), CodecError> {
    parse_json_value(bytes, usize::MAX).map(|_| ())
}

struct JsonSyntax<'a> {
    bytes: &'a [u8],
    index: usize,
}
impl JsonSyntax<'_> {
    fn invalid(message: &str) -> CodecError {
        CodecError::Invalid(message.into())
    }
    fn current(&self) -> Option<u8> {
        self.bytes.get(self.index).copied()
    }
    fn space(&mut self) {
        while self.current().is_some_and(|b| b" \t\r\n".contains(&b)) {
            self.index += 1;
        }
    }
    fn take(&mut self, byte: u8) -> bool {
        if self.current() == Some(byte) {
            self.index += 1;
            true
        } else {
            false
        }
    }
    fn string(&mut self) -> Result<String, CodecError> {
        let start = self.index;
        if !self.take(b'"') {
            return Err(Self::invalid("expected JSON string"));
        }
        while let Some(byte) = self.current() {
            self.index += 1;
            if byte == b'\\' {
                self.index += 1;
            } else if byte == b'"' {
                return Ok(serde_json::from_slice(&self.bytes[start..self.index])?);
            }
        }
        Err(Self::invalid("unterminated JSON string"))
    }
    fn value(&mut self, depth: usize) -> Result<Value, CodecError> {
        if depth > 128 {
            return Err(Self::invalid("JSON nesting exceeds 128"));
        }
        self.space();
        if self.current() == Some(b'"') {
            return self.string().map(Value::String);
        }
        if self.take(b'{') {
            let mut values = serde_json::Map::new();
            self.space();
            if self.take(b'}') {
                return Ok(Value::Object(values));
            }
            loop {
                self.space();
                let name = self.string()?;
                if values.contains_key(&name) {
                    return Err(Self::invalid("duplicate JSON key"));
                }
                self.space();
                if !self.take(b':') {
                    return Err(Self::invalid("expected JSON colon"));
                }
                values.insert(name, self.value(depth + 1)?);
                self.space();
                if self.take(b'}') {
                    return Ok(Value::Object(values));
                }
                if !self.take(b',') {
                    return Err(Self::invalid("expected JSON object separator"));
                }
            }
        }
        if self.take(b'[') {
            let mut values = Vec::new();
            self.space();
            if self.take(b']') {
                return Ok(Value::Array(values));
            }
            loop {
                values.push(self.value(depth + 1)?);
                self.space();
                if self.take(b']') {
                    return Ok(Value::Array(values));
                }
                if !self.take(b',') {
                    return Err(Self::invalid("expected JSON array separator"));
                }
            }
        }
        for (token, value) in [
            (b"true".as_slice(), Value::Bool(true)),
            (b"false".as_slice(), Value::Bool(false)),
            (b"null".as_slice(), Value::Null),
        ] {
            if self.bytes[self.index..].starts_with(token) {
                self.index += token.len();
                return Ok(value);
            }
        }
        self.number()
    }
    fn number(&mut self) -> Result<Value, CodecError> {
        let start = self.index;
        self.take(b'-');
        if !self.take(b'0') {
            if !self.current().is_some_and(|b| (b'1'..=b'9').contains(&b)) {
                return Err(Self::invalid("invalid JSON value"));
            }
            while self.current().is_some_and(|b| b.is_ascii_digit()) {
                self.index += 1;
            }
        }
        let mut float = false;
        if self.take(b'.') {
            float = true;
            if !self.current().is_some_and(|b| b.is_ascii_digit()) {
                return Err(Self::invalid("invalid JSON fraction"));
            }
            while self.current().is_some_and(|b| b.is_ascii_digit()) {
                self.index += 1;
            }
        }
        if self.take(b'e') || self.take(b'E') {
            float = true;
            if !self.take(b'+') {
                self.take(b'-');
            }
            if !self.current().is_some_and(|b| b.is_ascii_digit()) {
                return Err(Self::invalid("invalid JSON exponent"));
            }
            while self.current().is_some_and(|b| b.is_ascii_digit()) {
                self.index += 1;
            }
        }
        let token = std::str::from_utf8(&self.bytes[start..self.index]).expect("number is ASCII");
        if float
            && !token
                .parse::<f64>()
                .map_err(|_| Self::invalid("invalid JSON number"))?
                .is_finite()
        {
            return Err(Self::invalid("nonfinite JSON number"));
        }
        let number = if token == "-0" {
            serde_json::Number::from_f64(-0.0).expect("finite zero")
        } else {
            token.parse()?
        };
        Ok(Value::Number(number))
    }
}

/// Validate every nested float before serde_json's default NaN -> null behavior.
pub fn to_json_vec<T: Serialize + ?Sized>(value: &T) -> Result<Vec<u8>, CodecError> {
    value.serialize(FiniteSerializer)?;
    let bytes = serde_json::to_vec(value)?;
    validate_json(&bytes)?;
    Ok(bytes)
}

struct FiniteSerializer;
impl ser::Serializer for FiniteSerializer {
    type Ok = ();
    type Error = CodecError;
    type SerializeSeq = Self;
    type SerializeTuple = Self;
    type SerializeTupleStruct = Self;
    type SerializeTupleVariant = Self;
    type SerializeMap = Self;
    type SerializeStruct = Self;
    type SerializeStructVariant = Self;
    fn serialize_f32(self, value: f32) -> Result<(), CodecError> {
        self.serialize_f64(value.into())
    }
    fn serialize_f64(self, value: f64) -> Result<(), CodecError> {
        if value.is_finite() {
            Ok(())
        } else {
            Err(CodecError::Invalid("nonfinite float".into()))
        }
    }
    fn serialize_bool(self, _: bool) -> Result<(), CodecError> {
        Ok(())
    }
    fn serialize_i8(self, _: i8) -> Result<(), CodecError> {
        Ok(())
    }
    fn serialize_i16(self, _: i16) -> Result<(), CodecError> {
        Ok(())
    }
    fn serialize_i32(self, _: i32) -> Result<(), CodecError> {
        Ok(())
    }
    fn serialize_i64(self, _: i64) -> Result<(), CodecError> {
        Ok(())
    }
    fn serialize_u8(self, _: u8) -> Result<(), CodecError> {
        Ok(())
    }
    fn serialize_u16(self, _: u16) -> Result<(), CodecError> {
        Ok(())
    }
    fn serialize_u32(self, _: u32) -> Result<(), CodecError> {
        Ok(())
    }
    fn serialize_u64(self, _: u64) -> Result<(), CodecError> {
        Ok(())
    }
    fn serialize_i128(self, _: i128) -> Result<(), CodecError> {
        Ok(())
    }
    fn serialize_u128(self, _: u128) -> Result<(), CodecError> {
        Ok(())
    }
    fn serialize_char(self, _: char) -> Result<(), CodecError> {
        Ok(())
    }
    fn serialize_str(self, _: &str) -> Result<(), CodecError> {
        Ok(())
    }
    fn serialize_bytes(self, _: &[u8]) -> Result<(), CodecError> {
        Ok(())
    }
    fn serialize_none(self) -> Result<(), CodecError> {
        Ok(())
    }
    fn serialize_some<T: Serialize + ?Sized>(self, value: &T) -> Result<(), CodecError> {
        value.serialize(self)
    }
    fn serialize_unit(self) -> Result<(), CodecError> {
        Ok(())
    }
    fn serialize_unit_struct(self, _: &'static str) -> Result<(), CodecError> {
        Ok(())
    }
    fn serialize_unit_variant(
        self,
        _: &'static str,
        _: u32,
        _: &'static str,
    ) -> Result<(), CodecError> {
        Ok(())
    }
    fn serialize_newtype_struct<T: Serialize + ?Sized>(
        self,
        _: &'static str,
        value: &T,
    ) -> Result<(), CodecError> {
        value.serialize(self)
    }
    fn serialize_newtype_variant<T: Serialize + ?Sized>(
        self,
        _: &'static str,
        _: u32,
        _: &'static str,
        value: &T,
    ) -> Result<(), CodecError> {
        value.serialize(self)
    }
    fn serialize_seq(self, _: Option<usize>) -> Result<Self, CodecError> {
        Ok(self)
    }
    fn serialize_tuple(self, _: usize) -> Result<Self, CodecError> {
        Ok(self)
    }
    fn serialize_tuple_struct(self, _: &'static str, _: usize) -> Result<Self, CodecError> {
        Ok(self)
    }
    fn serialize_tuple_variant(
        self,
        _: &'static str,
        _: u32,
        _: &'static str,
        _: usize,
    ) -> Result<Self, CodecError> {
        Ok(self)
    }
    fn serialize_map(self, _: Option<usize>) -> Result<Self, CodecError> {
        Ok(self)
    }
    fn serialize_struct(self, _: &'static str, _: usize) -> Result<Self, CodecError> {
        Ok(self)
    }
    fn serialize_struct_variant(
        self,
        _: &'static str,
        _: u32,
        _: &'static str,
        _: usize,
    ) -> Result<Self, CodecError> {
        Ok(self)
    }
}
macro_rules! compound {
    ($trait:ident, $method:ident) => {
        impl ser::$trait for FiniteSerializer {
            type Ok = ();
            type Error = CodecError;
            fn $method<T: Serialize + ?Sized>(&mut self, value: &T) -> Result<(), CodecError> {
                value.serialize(FiniteSerializer)
            }
            fn end(self) -> Result<(), CodecError> {
                Ok(())
            }
        }
    };
}
compound!(SerializeSeq, serialize_element);
compound!(SerializeTuple, serialize_element);
compound!(SerializeTupleStruct, serialize_field);
compound!(SerializeTupleVariant, serialize_field);
impl ser::SerializeMap for FiniteSerializer {
    type Ok = ();
    type Error = CodecError;
    fn serialize_key<T: Serialize + ?Sized>(&mut self, value: &T) -> Result<(), CodecError> {
        value.serialize(FiniteSerializer)
    }
    fn serialize_value<T: Serialize + ?Sized>(&mut self, value: &T) -> Result<(), CodecError> {
        value.serialize(FiniteSerializer)
    }
    fn end(self) -> Result<(), CodecError> {
        Ok(())
    }
}
impl ser::SerializeStruct for FiniteSerializer {
    type Ok = ();
    type Error = CodecError;
    fn serialize_field<T: Serialize + ?Sized>(
        &mut self,
        _: &'static str,
        value: &T,
    ) -> Result<(), CodecError> {
        value.serialize(FiniteSerializer)
    }
    fn end(self) -> Result<(), CodecError> {
        Ok(())
    }
}
impl ser::SerializeStructVariant for FiniteSerializer {
    type Ok = ();
    type Error = CodecError;
    fn serialize_field<T: Serialize + ?Sized>(
        &mut self,
        _: &'static str,
        value: &T,
    ) -> Result<(), CodecError> {
        value.serialize(FiniteSerializer)
    }
    fn end(self) -> Result<(), CodecError> {
        Ok(())
    }
}

/// A raw ID is one URL path segment; reserved characters must never be routed.
pub fn encode_path_segment(id: &str) -> String {
    let mut encoded = String::new();
    for byte in id.as_bytes() {
        if byte.is_ascii_alphanumeric() || b"-._~".contains(byte) {
            encoded.push(*byte as char);
        } else {
            use std::fmt::Write;
            write!(encoded, "%{byte:02X}").expect("String write");
        }
    }
    encoded
}

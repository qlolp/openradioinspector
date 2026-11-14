//! Lossless, bounded measurement codecs. Socket policy, authentication, replay
//! and archive ownership remain the core's responsibility.
use ori_contracts::{CONTRACT_NAME, CodecError, UtcTimestamp, parse_json_value, to_json_vec};
use prost::Message;
use serde_json::{Map, Value, json};

pub const PROTOCOL_VERSION: u32 = 1;
pub const MAX_FRAME_BYTES: usize = 16 * 1024 * 1024 + 64 * 1024;
pub const MAX_POINTS: usize = 1_000_000;
pub const WS_PATH: &str = "/api/v1/events/ws";
pub const SUBPROTOCOL: &str = "ori.measurements.v1.protobuf";

#[derive(Debug, Clone, Copy)]
pub struct Limits {
    pub max_frame_bytes: usize,
    pub max_points: usize,
}
impl Default for Limits {
    fn default() -> Self {
        Self {
            max_frame_bytes: MAX_FRAME_BYTES,
            max_points: MAX_POINTS,
        }
    }
}
impl Limits {
    fn validate(self) -> Result<Self> {
        if self.max_frame_bytes == 0
            || self.max_frame_bytes > MAX_FRAME_BYTES
            || self.max_points > MAX_POINTS
        {
            return Err(StreamError::Invalid(
                "limits may only lower the protocol caps",
            ));
        }
        Ok(self)
    }
}

#[derive(Debug, thiserror::Error)]
pub enum StreamError {
    #[error("measurement frame exceeds {max_bytes} bytes")]
    TooLarge { max_bytes: usize },
    #[error("measurement sweep exceeds {max_points} points")]
    PointLimit { max_points: usize },
    #[error("invalid measurement stream: {0}")]
    Invalid(&'static str),
    #[error(transparent)]
    Json(#[from] CodecError),
    #[error("invalid protobuf: {0}")]
    Protobuf(#[from] prost::DecodeError),
}
pub type Result<T> = std::result::Result<T, StreamError>;

/// Checked-in equivalent of SpectrumArrays in the canonical proto IDL. Prost
/// encodes packed fields; the bounded pre-reader also admits legal unpacked or
/// segmented fields and rejects malformed values before Prost allocates vectors.
#[derive(Clone, PartialEq, Message)]
pub struct SpectrumArrays {
    #[prost(uint64, repeated, packed = "true", tag = "1")]
    pub frequencies_hz: Vec<u64>,
    #[prost(double, repeated, packed = "true", tag = "2")]
    pub levels: Vec<f64>,
}

mod wire {
    #[derive(Clone, PartialEq, prost::Message)]
    pub struct StreamFrame {
        #[prost(uint32, tag = "1")]
        pub protocol_version: u32,
        #[prost(uint64, tag = "2")]
        pub sequence: u64,
        #[prost(bytes = "vec", tag = "3")]
        pub envelope_json: Vec<u8>,
        #[prost(message, optional, tag = "4")]
        pub spectrum: Option<super::SpectrumArrays>,
    }
}

#[derive(Debug, Clone)]
pub struct DecodedFrame {
    pub protocol_version: u32,
    pub sequence: u64,
    /// Original full generic JSON, or stripped metadata JSON for a typed frame.
    pub envelope_json: Vec<u8>,
    pub envelope: Value,
    pub spectrum: Option<SpectrumArrays>,
}

/// Full strict validation for bridges forwarding the original frame. Packed
/// numeric values are checked without allocating their decoded vectors.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FrameHeader {
    pub protocol_version: u32,
    pub sequence: u64,
    pub has_spectrum: bool,
}

pub fn validate_frame(frame: &[u8]) -> Result<FrameHeader> {
    validate_frame_with_limits(frame, Limits::default())
}

pub fn validate_frame_with_limits(frame: &[u8], limits: Limits) -> Result<FrameHeader> {
    let (parts, _envelope) = validated(frame, limits)?;
    Ok(FrameHeader {
        protocol_version: PROTOCOL_VERSION,
        sequence: parts.sequence,
        has_spectrum: parts.spectrum,
    })
}
impl DecodedFrame {
    /// Reconstruct without serializing/parsing point arrays. The envelope and
    /// spectrum invariants validated by decode must not be changed beforehand.
    pub fn into_envelope(mut self) -> Value {
        if let Some(spectrum) = self.spectrum {
            let sweep = self
                .envelope
                .pointer_mut("/data/payload/sweep")
                .and_then(Value::as_object_mut)
                .expect("validated typed sweep metadata");
            sweep.insert(
                "frequencies_hz".into(),
                spectrum
                    .frequencies_hz
                    .into_iter()
                    .map(Value::from)
                    .collect(),
            );
            sweep.insert(
                "levels".into(),
                spectrum.levels.into_iter().map(Value::from).collect(),
            );
        }
        self.envelope
    }
}

/// Descriptive metadata only; routers must not advertise it until enabled.
pub fn capability() -> Value {
    json!({"version":PROTOCOL_VERSION,"transport":"websocket","encoding":"protobuf",
        "path":WS_PATH,"subprotocol":SUBPROTOCOL,"max_frame_bytes":MAX_FRAME_BYTES})
}

pub fn encode_frame(envelope_json: &[u8]) -> Result<Vec<u8>> {
    encode_frame_with_limits(envelope_json, Limits::default())
}

pub fn encode_frame_with_limits(envelope_json: &[u8], limits: Limits) -> Result<Vec<u8>> {
    let limits = limits.validate()?;
    // A smaller wire limit must still admit a larger JSON input when packing
    // makes it fit. The independent source JSON cap remains globally bounded.
    let mut envelope = parse_json_value(envelope_json, MAX_FRAME_BYTES)?;
    let sequence = envelope
        .get("data")
        .and_then(|v| v.get("sequence"))
        .and_then(Value::as_u64)
        .filter(|value| *value > 0)
        .ok_or(StreamError::Invalid("positive integer sequence required"))?;
    validate_envelope(&envelope, sequence, false, limits)?;
    let generic_length = 2
        + 1
        + varint_length(sequence)
        + 1
        + varint_length(envelope_json.len() as u64)
        + envelope_json.len();
    if let Some(spectrum) = take_spectrum(&mut envelope) {
        // Other workspace members enable serde_json/preserve_order. Typed
        // metadata has a canonical key order independent of feature unification
        // and of Map::remove's swap-removal under that feature. Generic frames
        // below still retain the exact original JSON bytes.
        envelope.sort_all_objects();
        let typed = wire::StreamFrame {
            protocol_version: PROTOCOL_VERSION,
            sequence,
            envelope_json: to_json_vec(&envelope)?,
            spectrum: Some(spectrum),
        };
        let size = typed.encoded_len();
        if size <= limits.max_frame_bytes && size < generic_length {
            return Ok(typed.encode_to_vec());
        }
    }
    if generic_length > limits.max_frame_bytes {
        return Err(StreamError::TooLarge {
            max_bytes: limits.max_frame_bytes,
        });
    }
    Ok(wire::StreamFrame {
        protocol_version: PROTOCOL_VERSION,
        sequence,
        envelope_json: envelope_json.to_vec(),
        spectrum: None,
    }
    .encode_to_vec())
}

pub fn decode_frame(frame: &[u8]) -> Result<DecodedFrame> {
    decode_frame_with_limits(frame, Limits::default())
}

pub fn decode_frame_with_limits(frame: &[u8], limits: Limits) -> Result<DecodedFrame> {
    let (_parts, envelope) = validated(frame, limits)?;
    let decoded = wire::StreamFrame::decode(frame)?;
    Ok(DecodedFrame {
        protocol_version: decoded.protocol_version,
        sequence: decoded.sequence,
        envelope_json: decoded.envelope_json,
        envelope,
        spectrum: decoded.spectrum,
    })
}

fn validated(frame: &[u8], limits: Limits) -> Result<(FrameParts<'_>, Value)> {
    let limits = limits.validate()?;
    if frame.len() > limits.max_frame_bytes {
        return Err(StreamError::TooLarge {
            max_bytes: limits.max_frame_bytes,
        });
    }
    let parts = scan_frame(frame, limits)?;
    let envelope = parse_json_value(parts.json, limits.max_frame_bytes)?;
    validate_envelope(&envelope, parts.sequence, parts.spectrum, limits)?;
    Ok((parts, envelope))
}

fn varint_length(mut value: u64) -> usize {
    let mut length = 1;
    while value >= 128 {
        length += 1;
        value >>= 7;
    }
    length
}

fn object(value: Option<&Value>) -> Result<&Map<String, Value>> {
    value
        .and_then(Value::as_object)
        .ok_or(StreamError::Invalid("expected object"))
}
fn string(value: Option<&Value>) -> Result<&str> {
    value
        .and_then(Value::as_str)
        .ok_or(StreamError::Invalid("expected string"))
}
fn timestamp(value: Option<&Value>) -> Result<()> {
    UtcTimestamp::try_from(string(value)?)?;
    Ok(())
}

fn validate_envelope(envelope: &Value, sequence: u64, typed: bool, limits: Limits) -> Result<()> {
    let root = object(Some(envelope))?;
    if root.get("contract").and_then(Value::as_str) != Some(CONTRACT_NAME)
        || root.get("contract_version").and_then(Value::as_u64) != Some(1)
    {
        return Err(StreamError::Invalid("unsupported contract"));
    }
    let data = object(root.get("data"))?;
    if data.get("sequence").and_then(Value::as_u64) != Some(sequence) || sequence == 0 {
        return Err(StreamError::Invalid("sequence mismatch"));
    }
    let kind = string(data.get("event_type"))?;
    timestamp(data.get("occurred_at"))?;
    if let Some(session_id) = data.get("session_id").filter(|v| !v.is_null()) {
        string(Some(session_id))?;
    }
    let payload = object(data.get("payload"))?;
    if typed && kind != "sweep.received" {
        return Err(StreamError::Invalid("spectrum requires sweep.received"));
    }
    if kind == "sweep.received" {
        let sweep = object(payload.get("sweep"))?;
        for name in ["id", "session_id", "stream_id", "level_unit"] {
            string(sweep.get(name))?;
        }
        let interval = object(sweep.get("interval"))?;
        timestamp(interval.get("started_at"))?;
        timestamp(interval.get("ended_at"))?;
        if let Some(settings) = sweep.get("settings").filter(|v| !v.is_null()) {
            object(Some(settings))?;
        }
        if typed {
            if sweep.contains_key("frequencies_hz") || sweep.contains_key("levels") {
                return Err(StreamError::Invalid("ambiguous metadata arrays"));
            }
        } else {
            let frequencies = sweep
                .get("frequencies_hz")
                .and_then(Value::as_array)
                .ok_or(StreamError::Invalid("sweep arrays"))?;
            let levels = sweep
                .get("levels")
                .and_then(Value::as_array)
                .ok_or(StreamError::Invalid("sweep arrays"))?;
            if frequencies.is_empty() || frequencies.len() != levels.len() {
                return Err(StreamError::Invalid("empty or unequal arrays"));
            }
            if frequencies.len() > limits.max_points {
                return Err(StreamError::PointLimit {
                    max_points: limits.max_points,
                });
            }
            if frequencies
                .iter()
                .chain(levels)
                .any(|value| !value.is_number())
            {
                return Err(StreamError::Invalid("numeric sweep arrays"));
            }
        }
    }
    Ok(())
}

/// Retain integer-vs-float token kinds on generic fallback. In particular, an
/// arbitrary precision Hz integer is never passed through binary64 conversion.
fn take_spectrum(envelope: &mut Value) -> Option<SpectrumArrays> {
    if envelope.get("data")?.get("event_type")?.as_str()? != "sweep.received" {
        return None;
    }
    let sweep = envelope
        .pointer_mut("/data/payload/sweep")?
        .as_object_mut()?;
    let frequencies_hz: Vec<u64> = sweep
        .get("frequencies_hz")?
        .as_array()?
        .iter()
        .map(Value::as_u64)
        .collect::<Option<_>>()?;
    if frequencies_hz.windows(2).any(|pair| pair[0] > pair[1]) {
        return None;
    }
    let levels: Vec<f64> = sweep
        .get("levels")?
        .as_array()?
        .iter()
        .map(|value| {
            let number = value.as_number()?;
            if !number.is_f64() {
                return None;
            }
            number.as_f64().filter(|v| v.is_finite())
        })
        .collect::<Option<_>>()?;
    sweep.remove("frequencies_hz");
    sweep.remove("levels");
    Some(SpectrumArrays {
        frequencies_hz,
        levels,
    })
}

struct Reader<'a> {
    bytes: &'a [u8],
    position: usize,
}
impl<'a> Reader<'a> {
    fn new(bytes: &'a [u8]) -> Self {
        Self { bytes, position: 0 }
    }
    fn done(&self) -> bool {
        self.position == self.bytes.len()
    }
    fn varint(&mut self) -> Result<u64> {
        let mut value = 0_u64;
        for index in 0..10 {
            let byte = *self
                .bytes
                .get(self.position)
                .ok_or(StreamError::Invalid("truncated varint"))?;
            self.position += 1;
            if index == 9 && byte > 1 {
                return Err(StreamError::Invalid("overflowing varint"));
            }
            value |= u64::from(byte & 127) << (index * 7);
            if byte < 128 {
                return Ok(value);
            }
        }
        Err(StreamError::Invalid("unterminated varint"))
    }
    fn tag(&mut self) -> Result<(u32, u8)> {
        let key = self.varint()?;
        if !(8..=u64::from(u32::MAX)).contains(&key) {
            return Err(StreamError::Invalid("invalid field tag"));
        }
        Ok(((key >> 3) as u32, (key & 7) as u8))
    }
    fn take(&mut self, length: usize) -> Result<&'a [u8]> {
        if length > self.bytes.len() - self.position {
            return Err(StreamError::Invalid("truncated field"));
        }
        let result = &self.bytes[self.position..self.position + length];
        self.position += length;
        Ok(result)
    }
    fn blob(&mut self) -> Result<&'a [u8]> {
        let length = self.varint()?;
        if length > (self.bytes.len() - self.position) as u64 {
            return Err(StreamError::Invalid("truncated length-delimited field"));
        }
        self.take(length as usize)
    }
    fn double(&mut self) -> Result<f64> {
        Ok(f64::from_le_bytes(
            self.take(8)?.try_into().expect("eight bounded bytes"),
        ))
    }
    fn skip(&mut self, wire: u8) -> Result<()> {
        match wire {
            0 => {
                self.varint()?;
            }
            1 => {
                self.take(8)?;
            }
            2 => {
                self.blob()?;
            }
            5 => {
                self.take(4)?;
            }
            _ => return Err(StreamError::Invalid("unsupported protobuf wire type")),
        }
        Ok(())
    }
}

struct FrameParts<'a> {
    sequence: u64,
    json: &'a [u8],
    spectrum: bool,
}
fn scan_frame(frame: &[u8], limits: Limits) -> Result<FrameParts<'_>> {
    let mut reader = Reader::new(frame);
    let mut seen = [false; 4];
    let mut version = None;
    let mut sequence = None;
    let mut json = None;
    let mut spectrum = false;
    while !reader.done() {
        let (field, wire) = reader.tag()?;
        if (1..=4).contains(&field) {
            let index = field as usize - 1;
            if seen[index] {
                return Err(StreamError::Invalid("duplicate unique field"));
            }
            seen[index] = true;
            if (field <= 2 && wire != 0) || (field >= 3 && wire != 2) {
                return Err(StreamError::Invalid("header field wire type"));
            }
            match field {
                1 => version = Some(reader.varint()?),
                2 => sequence = Some(reader.varint()?),
                3 => json = Some(reader.blob()?),
                4 => {
                    scan_spectrum(reader.blob()?, limits.max_points)?;
                    spectrum = true;
                }
                _ => unreachable!(),
            }
        } else {
            reader.skip(wire)?;
        }
    }
    if version != Some(u64::from(PROTOCOL_VERSION)) {
        return Err(StreamError::Invalid(
            "missing or unsupported protocol version",
        ));
    }
    let sequence = sequence
        .filter(|v| *v > 0)
        .ok_or(StreamError::Invalid("positive sequence required"))?;
    let json = json
        .filter(|v| !v.is_empty())
        .ok_or(StreamError::Invalid("missing envelope JSON"))?;
    Ok(FrameParts {
        sequence,
        json,
        spectrum,
    })
}

fn scan_spectrum(bytes: &[u8], max_points: usize) -> Result<()> {
    let mut reader = Reader::new(bytes);
    let mut frequencies = 0;
    let mut levels = 0;
    let mut previous = 0;
    let mut frequency = |value: u64| -> Result<()> {
        frequencies += 1;
        if frequencies > max_points {
            return Err(StreamError::PointLimit { max_points });
        }
        if frequencies > 1 && value < previous {
            return Err(StreamError::Invalid("unordered frequencies"));
        }
        previous = value;
        Ok(())
    };
    let mut level = |value: f64| -> Result<()> {
        levels += 1;
        if levels > max_points {
            return Err(StreamError::PointLimit { max_points });
        }
        if !value.is_finite() {
            return Err(StreamError::Invalid("nonfinite level"));
        }
        Ok(())
    };
    while !reader.done() {
        let (field, wire) = reader.tag()?;
        match (field, wire) {
            (1, 0) => frequency(reader.varint()?)?,
            (1, 2) => {
                let mut packed = Reader::new(reader.blob()?);
                while !packed.done() {
                    frequency(packed.varint()?)?;
                }
            }
            (2, 1) => level(reader.double()?)?,
            (2, 2) => {
                let mut packed = Reader::new(reader.blob()?);
                if !packed.bytes.len().is_multiple_of(8) {
                    return Err(StreamError::Invalid("packed double length"));
                }
                while !packed.done() {
                    level(packed.double()?)?;
                }
            }
            (1 | 2, _) => return Err(StreamError::Invalid("spectrum field wire type")),
            _ => reader.skip(wire)?,
        }
    }
    if frequencies == 0 || frequencies != levels {
        return Err(StreamError::Invalid("empty or unequal arrays"));
    }
    Ok(())
}

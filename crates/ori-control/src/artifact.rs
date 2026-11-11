use crate::*;
use base64::{Engine, engine::general_purpose::STANDARD};
use chrono::{DateTime, SecondsFormat, Utc};
use ori_contracts::to_json_vec;
use ori_instruments::{OperationInterval, RawSpectrumSample, SpectrumKind};
use rand::RngCore;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

pub const MAX_BLOB_BYTES: usize = 8 * 1024 * 1024;
pub(crate) fn new_id(prefix: &str) -> String {
    let mut bytes = [0u8; 16];
    rand::rngs::OsRng.fill_bytes(&mut bytes);
    format!("{prefix}{}", hex(&bytes))
}
pub(crate) fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}
pub(crate) fn time(at: DateTime<Utc>) -> String {
    at.to_rfc3339_opts(SecondsFormat::Micros, true)
}
impl ClosedArtifact {
    pub fn from_payload(
        target: Target,
        kind: ArtifactKind,
        mime: &str,
        filename: &str,
        payload: Vec<u8>,
        metadata: Value,
        max_bytes: usize,
    ) -> Result<Self> {
        if !matches!(
            mime,
            "image/png"
                | "application/octet-stream"
                | "application/json"
                | "text/csv"
                | "audio/wav"
        ) || !crate::request::identifier(filename, 1, 80)
            || !filename.as_bytes()[0].is_ascii_alphanumeric()
            || filename.contains("..")
        {
            return Err(ControlError::Invalid("invalid artifact MIME/filename"));
        }
        if payload.len() > max_bytes.min(MAX_BLOB_BYTES) {
            return Err(ControlError::Invalid("artifact exceeds bounded payload"));
        }
        if !metadata.is_object()
            || to_json_vec(&metadata)
                .map_err(|_| ControlError::Invalid("invalid finite artifact metadata"))?
                .len()
                > 65536
        {
            return Err(ControlError::Invalid("artifact metadata exceeds 64 KiB"));
        }
        DateTime::parse_from_rfc3339(
            metadata["occurred_at"]
                .as_str()
                .ok_or(ControlError::Invalid("artifact creation UTC missing"))?,
        )
        .map_err(|_| ControlError::Invalid("invalid artifact creation UTC"))?;
        let sha256 = hex(&Sha256::digest(&payload));
        Ok(Self {
            id: new_id("instrument-"),
            target,
            kind,
            mime_type: mime.into(),
            filename: filename.into(),
            payload,
            sha256,
            metadata,
        })
    }
    pub fn summary(&self) -> Value {
        let at = DateTime::parse_from_rfc3339(
            self.metadata["occurred_at"]
                .as_str()
                .expect("closed artifact UTC"),
        )
        .expect("validated UTC")
        .to_utc();
        json!({"id":self.id,"session_id":self.target.session_id,"stream_id":self.target.stream_id,"schema_version":1,"kind":self.kind,"mime_type":self.mime_type,"filename":self.filename,"sha256":self.sha256,"created_at":time(at),"size_bytes":self.payload.len(),"metadata":self.metadata})
    }
    pub fn preview(&self) -> Value {
        json!({"kind":self.kind,"mime_type":self.mime_type,"filename":self.filename,"data_base64":STANDARD.encode(&self.payload),"sha256":self.sha256,"metadata":self.metadata})
    }
}
pub fn screen_png(raw: &[u8]) -> Result<Vec<u8>> {
    if raw.len() != 480 * 320 * 2 {
        return Err(ControlError::Invalid("incomplete RGB565BE display"));
    }
    let rgb: Vec<u8> = raw
        .as_chunks::<2>()
        .0
        .iter()
        .flat_map(|p| {
            let w = u16::from_be_bytes([p[0], p[1]]) as u32;
            [
                ((w >> 11) & 31) * 255 / 31,
                ((w >> 5) & 63) * 255 / 63,
                (w & 31) * 255 / 31,
            ]
            .map(|v| v as u8)
        })
        .collect();
    let mut bytes = Vec::new();
    {
        let mut encoder = png::Encoder::new(&mut bytes, 480, 320);
        encoder.set_color(png::ColorType::Rgb);
        encoder.set_depth(png::BitDepth::Eight);
        encoder.set_filter(png::Filter::NoFilter);
        let mut writer = encoder
            .write_header()
            .map_err(|_| ControlError::Backend("PNG encoder unavailable".into()))?;
        writer
            .write_image_data(&rgb)
            .map_err(|_| ControlError::Backend("PNG encoding failed".into()))?;
    }
    Ok(bytes)
}
pub fn raw_document(raw: &RawSpectrumSample) -> Result<Value> {
    if raw.frequencies_hz.is_empty()
        || raw.frequencies_hz.len() != raw.levels.len()
        || raw.frequencies_hz.len() > 65536
        || raw.levels.iter().any(|v| !v.is_finite())
        || raw.frequencies_hz.windows(2).any(|p| p[0] > p[1])
        || raw.level_unit != "dBm"
        || raw.interval.ended_at < raw.interval.started_at
    {
        return Err(ControlError::Invalid("invalid source scanraw sample"));
    }
    Ok(
        json!({"frequencies_hz":raw.frequencies_hz,"levels":raw.levels,"level_unit":raw.level_unit,"wire_base64":STANDARD.encode(&raw.wire),"zero_db":raw.zero_db,"started_at":time(raw.interval.started_at),"ended_at":time(raw.interval.ended_at),"sample_offsets_seconds":null,"timing_source":"interval_only","grid_source":"firmware_ea71dfd_scanraw","requested_stop_hz":raw.settings.get("requested_stop_hz").cloned().unwrap_or(Value::Null),"stop_is_inclusive":if raw.kind==SpectrumKind::ZeroSpan{Value::Null}else{json!(false)}}),
    )
}
pub(crate) fn provenance(
    status: &Value,
    revision: u64,
    interval: &OperationInterval,
    timing: &str,
) -> Value {
    let connection = status
        .get("connection_id")
        .and_then(Value::as_str)
        .unwrap_or("unknown");
    json!({"profile":ori_instruments::CONTROL_PROFILE,"revision":revision,"settings":status.get("settings").cloned().unwrap_or_else(||json!({})),"connection_id":connection,"segment_id":format!("{connection}:{revision}"),"device_mode":status.get("device_mode").cloned().unwrap_or_else(||json!("unknown")),"device_mode_readback":false,"settings_source":status.get("settings_source").cloned().unwrap_or_else(||json!("unknown")),"settings_readback":false,"settings_complete":false,"hardware_validation":"not_run","occurred_at":time(Utc::now()),"started_at":time(interval.started_at),"ended_at":time(interval.ended_at),"timing_source":timing})
}
/// Validate a closed, mono PCM16 WAV produced by Core without changing bytes.
pub fn validate_wav(bytes: &[u8], rate: u32) -> Result<()> {
    let invalid = || ControlError::Invalid("complete mono PCM16 WAV required");
    if bytes.len() < 44
        || bytes.len() > MAX_BLOB_BYTES
        || &bytes[..4] != b"RIFF"
        || &bytes[8..12] != b"WAVE"
        || u32::from_le_bytes(bytes[4..8].try_into().unwrap()) as usize + 8 != bytes.len()
    {
        return Err(invalid());
    }
    let mut at = 12usize;
    let mut format = false;
    let mut data = false;
    while at < bytes.len() {
        if at + 8 > bytes.len() {
            return Err(invalid());
        }
        let len = u32::from_le_bytes(bytes[at + 4..at + 8].try_into().unwrap()) as usize;
        let end = at
            .checked_add(8)
            .and_then(|n| n.checked_add(len))
            .filter(|n| *n <= bytes.len())
            .ok_or_else(invalid)?;
        match &bytes[at..at + 4] {
            b"fmt " => {
                if format || len < 16 {
                    return Err(invalid());
                }
                let f = &bytes[at + 8..end];
                if u16::from_le_bytes(f[..2].try_into().unwrap()) != 1
                    || u16::from_le_bytes(f[2..4].try_into().unwrap()) != 1
                    || u32::from_le_bytes(f[4..8].try_into().unwrap()) != rate
                    || u32::from_le_bytes(f[8..12].try_into().unwrap()) != rate * 2
                    || u16::from_le_bytes(f[12..14].try_into().unwrap()) != 2
                    || u16::from_le_bytes(f[14..16].try_into().unwrap()) != 16
                {
                    return Err(invalid());
                }
                format = true;
            }
            b"data" => {
                if data || len == 0 || !len.is_multiple_of(2) {
                    return Err(invalid());
                }
                data = true;
            }
            _ => {}
        }
        at = end + len % 2;
        if at > bytes.len() {
            return Err(invalid());
        }
    }
    if !format || !data {
        return Err(invalid());
    }
    Ok(())
}

pub(crate) fn validate_wav_duration(bytes: &[u8], parameters: &AudioParameters) -> Result<()> {
    // Called only after strict RIFF validation; inspect the original data chunk.
    let mut at = 12;
    while at < bytes.len() {
        let len = u32::from_le_bytes(bytes[at + 4..at + 8].try_into().unwrap()) as usize;
        if &bytes[at..at + 4] == b"data" {
            let expected =
                (parameters.seconds * parameters.sample_rate_hz as f64).round_ties_even() as usize;
            if len / 2 != expected {
                return Err(ControlError::Invalid(
                    "incomplete requested analog audio interval",
                ));
            }
            return Ok(());
        }
        at += 8 + len + len % 2;
    }
    Err(ControlError::Invalid("missing WAV data"))
}

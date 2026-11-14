//! Complete immutable audio validation; the archive commits bytes and summary event together.
use crate::error::{ApiError, ApiResult};
use base64::{Engine, engine::general_purpose::STANDARD};
use ori_archive::Artifact;
use ori_contracts::UtcTimestamp;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

fn invalid(message: &str) -> ApiError {
    ApiError::invalid(message)
}
pub(crate) fn audio(session: &str, payload: Value) -> ApiResult<Artifact> {
    let required = [
        "id",
        "stream_id",
        "frequency_hz",
        "mode",
        "interval",
        "data_base64",
        "sha256",
    ];
    if payload.as_object().is_none_or(|map| {
        map.len() != required.len() || required.iter().any(|key| !map.contains_key(*key))
    }) {
        return Err(invalid(
            "Audio segment requires id, stream_id, frequency_hz, mode, interval, data_base64 and sha256",
        ));
    }
    for field in ["id", "stream_id", "mode"] {
        if payload[field]
            .as_str()
            .is_none_or(|text| text.trim().is_empty() || text.chars().count() > 128)
        {
            return Err(invalid(&format!("Invalid audio segment {field}")));
        }
    }
    let positive_integer = payload["frequency_hz"]
        .as_number()
        .map(ToString::to_string)
        .is_some_and(|text| text != "0" && text.bytes().all(|byte| byte.is_ascii_digit()));
    if !positive_integer {
        return Err(invalid("Audio frequency must be positive integer Hz"));
    }
    let timestamp = |field: &str| -> ApiResult<UtcTimestamp> {
        let raw = payload["interval"][field]
            .as_str()
            .filter(|text| !text.trim().is_empty())
            .ok_or_else(|| invalid(&format!("{field} должно быть ISO-строкой")))?;
        if raw
            .get(10..)
            .is_none_or(|time| !time.contains(['+', '-', 'Z']))
        {
            return Err(invalid(&format!("{field} должно содержать часовой пояс")));
        }
        raw.parse()
            .map_err(|_| invalid(&format!("Некорректное {field}: {raw}")))
    };
    let start = timestamp("started_at")?;
    let end = timestamp("ended_at")?;
    if end < start {
        return Err(invalid("ended_at не может предшествовать started_at"));
    }
    let encoded = payload["data_base64"]
        .as_str()
        .filter(|text| text.len() <= 16 * 1024 * 1024)
        .ok_or_else(|| invalid("Audio segment exceeds request budget"))?;
    let raw = STANDARD
        .decode(encoded)
        .map_err(|_| invalid("Invalid audio base64"))?;
    let digest = Sha256::digest(&raw)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    if payload["sha256"] != digest {
        return Err(invalid("Audio segment checksum mismatch"));
    }
    let (rate, frames) = wav(&raw)?;
    let duration = i128::from(end.unix_micros()) - i128::from(start.unix_micros());
    if (duration * i128::from(rate) - i128::from(frames) * 1_000_000).abs() > i128::from(rate) {
        return Err(invalid("Audio interval does not match PCM frame duration"));
    }
    let metadata = json!({"frequency_hz":payload["frequency_hz"],"mode":payload["mode"],"interval":{"started_at":start.as_str(),"ended_at":end.as_str()},"sample_rate":rate,"frame_count":frames,"occurred_at":start.as_str()});
    Ok(Artifact {
        id: payload["id"].as_str().expect("validated ID").into(),
        session_id: session.into(),
        stream_id: payload["stream_id"]
            .as_str()
            .expect("validated stream")
            .into(),
        schema_version: 1,
        kind: "audio".into(),
        mime_type: "audio/wav".into(),
        filename: format!("audio-{}.wav", &digest[..24]),
        sha256: digest,
        created_at: start.to_string(),
        payload: raw,
        metadata,
    })
}
fn u16le(bytes: &[u8], offset: usize) -> Option<u16> {
    Some(u16::from_le_bytes(
        bytes.get(offset..offset + 2)?.try_into().ok()?,
    ))
}
fn u32le(bytes: &[u8], offset: usize) -> Option<u32> {
    Some(u32::from_le_bytes(
        bytes.get(offset..offset + 4)?.try_into().ok()?,
    ))
}
fn wav(raw: &[u8]) -> ApiResult<(u32, u64)> {
    if raw.len() < 44
        || raw.get(..4) != Some(b"RIFF")
        || raw.get(8..12) != Some(b"WAVE")
        || u32le(raw, 4)
            .map(u64::from)
            .is_none_or(|size| size + 8 != raw.len() as u64)
    {
        return Err(invalid(
            "Audio segment must contain a complete RIFF/WAV file",
        ));
    }
    let mut offset = 12;
    let mut format = None;
    let mut data = None;
    while offset < raw.len() {
        if raw.len() - offset < 8 {
            return Err(invalid("Incomplete WAV chunk header"));
        }
        let size = u32le(raw, offset + 4).expect("bounded chunk header") as usize;
        let end = offset
            .checked_add(8)
            .and_then(|v| v.checked_add(size))
            .ok_or_else(|| invalid("Incomplete WAV chunk or padding"))?;
        let next = end
            .checked_add(size % 2)
            .filter(|end| *end <= raw.len())
            .ok_or_else(|| invalid("Incomplete WAV chunk or padding"))?;
        match &raw[offset..offset + 4] {
            b"fmt " => {
                let fields = &raw[offset + 8..end];
                if size < 16 {
                    return Err(invalid("Incomplete WAV format"));
                }
                let rate = u32le(fields, 4).expect("complete format");
                if u16le(fields, 2) != Some(1)
                    || u16le(fields, 14) != Some(16)
                    || u16le(fields, 12) != Some(2)
                    || rate.checked_mul(2) != u32le(fields, 8)
                {
                    return Err(invalid("WAV format must consistently declare mono PCM16"));
                }
                if u16le(fields, 0) == Some(0xfffe)
                    && (size < 40
                        || u16le(fields, 16)
                            .is_none_or(|extension| usize::from(extension) + 18 > size))
                {
                    return Err(invalid("Incomplete extensible WAV format"));
                }
                if format.replace(fields).is_some() {
                    return Err(invalid(
                        "WAV must have exactly one format and one data chunk",
                    ));
                }
            }
            b"data" => {
                if format.is_none() {
                    return Err(invalid("Invalid PCM WAV audio"));
                }
                if data.replace(size).is_some() {
                    return Err(invalid(
                        "WAV must have exactly one format and one data chunk",
                    ));
                }
            }
            _ => {}
        }
        offset = next;
    }
    let fmt =
        format.ok_or_else(|| invalid("WAV must have exactly one format and one data chunk"))?;
    let bytes =
        data.ok_or_else(|| invalid("WAV must have exactly one format and one data chunk"))?;
    let format = u16le(fmt, 0).ok_or_else(|| invalid("Invalid PCM WAV audio"))?;
    let channels = u16le(fmt, 2);
    let rate = u32le(fmt, 4).ok_or_else(|| invalid("Invalid PCM WAV audio"))?;
    let bits = u16le(fmt, 14);
    let pcm_guid = [1, 0, 0, 0, 0, 0, 16, 0, 128, 0, 0, 170, 0, 56, 155, 113];
    if format != 1 && (format != 0xfffe || fmt.get(24..40) != Some(pcm_guid.as_slice())) {
        return Err(invalid("Invalid PCM WAV audio"));
    }
    if channels != Some(1) || bits != Some(16) {
        return Err(invalid("Audio must be uncompressed mono PCM16"));
    }
    if !(1..=192000).contains(&rate) || bytes == 0 || bytes % 2 != 0 {
        return Err(invalid(
            "Incomplete audio frames or unsupported sample rate",
        ));
    }
    Ok((rate, (bytes / 2) as u64))
}
pub(crate) fn summary(item: &Artifact) -> Value {
    json!({"id":item.id,"session_id":item.session_id,"stream_id":item.stream_id,"schema_version":item.schema_version,"kind":item.kind,"mime_type":item.mime_type,"filename":item.filename,"sha256":item.sha256,"created_at":item.created_at,"size_bytes":item.payload.len(),"metadata":item.metadata})
}

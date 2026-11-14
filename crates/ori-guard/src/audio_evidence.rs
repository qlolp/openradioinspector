//! Read-only exact-frame export and separate immutable operator range marks.
//! Neither operation opens a device, changes a tune or rewrites original audio.
use crate::{GuardError, GuardService, Result, util::*};
use ori_archive::{Artifact, EventDraft, Mutation};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

const MAX_WAV: usize = 16 * 1024 * 1024;
const MAX_PROVENANCE: usize = 65536;
pub struct AudioClip {
    pub bytes: Vec<u8>,
    pub sha256: String,
}
struct Pcm<'a> {
    rate: u32,
    frames: u64,
    data: &'a [u8],
}
fn u16le(raw: &[u8], at: usize) -> Option<u16> {
    Some(u16::from_le_bytes(raw.get(at..at + 2)?.try_into().ok()?))
}
fn u32le(raw: &[u8], at: usize) -> Option<u32> {
    Some(u32::from_le_bytes(raw.get(at..at + 4)?.try_into().ok()?))
}
fn pcm(raw: &[u8]) -> Result<Pcm<'_>> {
    if raw.len() < 44
        || raw.len() > MAX_WAV
        || raw.get(..4) != Some(b"RIFF")
        || raw.get(8..12) != Some(b"WAVE")
        || u32le(raw, 4).map(u64::from) != Some(raw.len() as u64 - 8)
    {
        return Err(invalid("Нужен полный WAV RIFF/WAVE до 16 МиБ"));
    }
    let mut at = 12usize;
    let mut format = None;
    let mut data = None;
    while at < raw.len() {
        let length = u32le(raw, at + 4).ok_or_else(|| invalid("Неполный заголовок WAV"))? as usize;
        let end = at
            .checked_add(8)
            .and_then(|n| n.checked_add(length))
            .ok_or_else(|| invalid("Размер WAV"))?;
        let next = end
            .checked_add(length % 2)
            .filter(|n| *n <= raw.len())
            .ok_or_else(|| invalid("Неполный WAV или padding"))?;
        match raw.get(at..at + 4) {
            Some(b"fmt ") => {
                let fields = &raw[at + 8..end];
                if format.is_some() || fields.len() < 16 {
                    return Err(invalid("Нужен единственный формат PCM16 WAV"));
                }
                let rate = u32le(fields, 4).ok_or_else(|| invalid("Частота PCM"))?;
                let code = u16le(fields, 0);
                let pcm_guid = [1, 0, 0, 0, 0, 0, 16, 0, 128, 0, 0, 170, 0, 56, 155, 113];
                let extensible = code == Some(0xfffe)
                    && fields.len() >= 40
                    && u16le(fields, 16)
                        .is_some_and(|n| n >= 22 && usize::from(n) + 18 <= fields.len())
                    && u16le(fields, 18) == Some(16)
                    && fields.get(24..40) == Some(pcm_guid.as_slice());
                if !(code == Some(1) || extensible)
                    || u16le(fields, 2) != Some(1)
                    || u16le(fields, 14) != Some(16)
                    || u16le(fields, 12) != Some(2)
                    || !(1..=192000).contains(&rate)
                    || rate.checked_mul(2) != u32le(fields, 8)
                {
                    return Err(invalid(
                        "Экспорт доступен для согласованного mono PCM16 без преобразования",
                    ));
                }
                format = Some(rate);
            }
            Some(b"data") => {
                if format.is_none() || data.is_some() || length == 0 || !length.is_multiple_of(2) {
                    return Err(invalid(
                        "Нужен единственный полный блок PCM после формата WAV",
                    ));
                }
                data = Some(&raw[at + 8..end]);
            }
            _ => {}
        }
        at = next;
    }
    let rate = format.ok_or_else(|| invalid("Нет формата WAV"))?;
    let data = data.ok_or_else(|| invalid("Нет PCM WAV"))?;
    Ok(Pcm {
        rate,
        frames: (data.len() / 2) as u64,
        data,
    })
}
fn checksum(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}
fn checked_source(item: &Artifact) -> Result<Pcm<'_>> {
    if item.sha256 != checksum(&item.payload) {
        return Err(invalid("Контрольная сумма исходной фонограммы не совпала"));
    }
    pcm(&item.payload)
}
fn same_source(item: &Artifact, expected: &str, expected_id: &str) -> Result<()> {
    if expected.len() != 64
        || !expected
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
    {
        return Err(invalid("Нужна исходная SHA-256 фонограммы"));
    }
    if item.sha256 != expected || item.id != expected_id {
        return Err(GuardError::Conflict(
            "Фонограмма события изменилась; обновите сведения".into(),
        ));
    }
    Ok(())
}
fn range(source: &Pcm<'_>, start: u64, end: u64) -> Result<()> {
    if start >= end || end > source.frames {
        return Err(invalid(
            "Нужен непустой диапазон отсчётов [начало, конец) внутри исходной записи",
        ));
    }
    Ok(())
}
fn provenance(item: &Artifact, event: &str, source: &Pcm<'_>, start: u64, end: u64) -> Value {
    json!({"schema":"ori-audio-clip-v1","source_artifact_id":item.id,"source_sha256":item.sha256,
        "session_id":item.session_id,"stream_id":item.stream_id,"event_id":event,
        "sample_rate_hz":source.rate,"channels":1,"encoding":"pcm16_le","source_frame_count":source.frames,
        "start_frame":start,"end_frame":end,"source_context":item.metadata})
}
fn chunk(out: &mut Vec<u8>, kind: &[u8; 4], bytes: &[u8]) {
    out.extend(kind);
    out.extend((bytes.len() as u32).to_le_bytes());
    out.extend(bytes);
    if !bytes.len().is_multiple_of(2) {
        out.push(0);
    }
}
impl GuardService {
    pub async fn event_audio_info(&self, project: &str, event: &str) -> Result<Value> {
        let item = self.event_audio(project, event).await?;
        let source = checked_source(&item)?;
        Ok(
            json!({"artifact_id":item.id,"sha256":item.sha256,"frame_count":source.frames,
            "sample_rate_hz":source.rate,"channels":1,"encoding":"pcm16_le","source_context":item.metadata}),
        )
    }
    pub async fn event_audio_clip(
        &self,
        project: &str,
        event: &str,
        start: u64,
        end: u64,
        expected_sha256: &str,
        expected_id: &str,
    ) -> Result<AudioClip> {
        let item = self.event_audio(project, event).await?;
        same_source(&item, expected_sha256, expected_id)?;
        let source = checked_source(&item)?;
        range(&source, start, end)?;
        let context = ori_contracts::to_json_vec(&provenance(&item, event, &source, start, end))
            .map_err(|_| invalid("Метаданные фонограммы"))?;
        if context.len() > MAX_PROVENANCE {
            return Err(invalid("Контекст экспорта превышает 64 КиБ"));
        }
        let data = &source.data[start as usize * 2..end as usize * 2];
        let size = 44 + 8 + context.len() + context.len() % 2 + data.len();
        if size > MAX_WAV {
            return Err(invalid("Фрагмент вместе с контекстом превышает 16 МиБ"));
        }
        let mut bytes = Vec::with_capacity(size);
        bytes.extend(b"RIFF");
        bytes.extend(((size - 8) as u32).to_le_bytes());
        bytes.extend(b"WAVE");
        let mut fmt = Vec::with_capacity(16);
        fmt.extend(1u16.to_le_bytes());
        fmt.extend(1u16.to_le_bytes());
        fmt.extend(source.rate.to_le_bytes());
        fmt.extend((source.rate * 2).to_le_bytes());
        fmt.extend(2u16.to_le_bytes());
        fmt.extend(16u16.to_le_bytes());
        chunk(&mut bytes, b"fmt ", &fmt);
        chunk(&mut bytes, b"ori ", &context);
        chunk(&mut bytes, b"data", data);
        Ok(AudioClip {
            sha256: checksum(&bytes),
            bytes,
        })
    }
    pub async fn add_audio_mark(
        &self,
        project: &str,
        event: &str,
        payload: Value,
    ) -> Result<Value> {
        fields(
            &payload,
            &[
                "start_frame",
                "end_frame",
                "label",
                "source_sha256",
                "source_artifact_id",
            ],
            true,
        )?;
        let start = integer(&payload, "start_frame")?;
        let end = integer(&payload, "end_frame")?;
        let label = s(&payload, "label")?.trim();
        if label.is_empty() || label.chars().count() > 200 {
            return Err(invalid("Подпись отметки должна содержать 1–200 символов"));
        }
        let _tx = self.coordinator.lock().await;
        self.ensure_open()?;
        let item = self.event_audio(project, event).await?;
        same_source(
            &item,
            s(&payload, "source_sha256")?,
            &text(&payload, "source_artifact_id", 200)?,
        )?;
        let source = checked_source(&item)?;
        range(&source, start, end)?;
        let mark = self.record(
            Some(project),
            json!({"event_id":event,"source_artifact_id":item.id,"source_sha256":item.sha256,
            "start_frame":start,"end_frame":end,"sample_rate_hz":source.rate,"label":label}),
        )?;
        self.archive
            .apply(vec![
                Self::mutation("audio-mark", mark.clone()),
                Mutation::Event(EventDraft {
                    event_type: "monitoring.audio_mark".into(),
                    occurred_at: self.clock.now().as_str().into(),
                    session_id: Some(item.session_id),
                    payload: mark.clone(),
                }),
            ])
            .await?;
        Ok(mark)
    }
    pub async fn audio_marks(
        &self,
        project: &str,
        event: &str,
        after: &str,
        limit: usize,
    ) -> Result<Value> {
        self.project(project).await?;
        self.event_record(project, event).await?;
        if !(1..=64).contains(&limit) || after.len() > 1024 {
            return Err(invalid("Неверная страница аудиоотметок"));
        }
        value(
            &self
                .archive
                .audio_marks(project, event, after, limit)
                .await?,
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn wav() -> Vec<u8> {
        let mut out = b"RIFF".to_vec();
        out.extend(38u32.to_le_bytes());
        out.extend(b"WAVE");
        let fmt = [1, 0, 1, 0, 64, 31, 0, 0, 128, 62, 0, 0, 2, 0, 16, 0];
        chunk(&mut out, b"fmt ", &fmt);
        chunk(&mut out, b"data", &[0xff, 0x7f]);
        out
    }
    #[test]
    fn malformed_pcm_is_not_repaired_or_converted() {
        let original = wav();
        assert_eq!(pcm(&original).unwrap().frames, 1);
        for (at, replacement) in [
            (20, 3),
            (22, 2),
            (32, 4),
            (34, 8),
            (24, 0),
            (28, 1),
            (40, 1),
        ] {
            let mut bad = original.clone();
            bad[at] = replacement;
            assert!(pcm(&bad).is_err(), "offset {at}");
        }
        for removed in 1..=10 {
            assert!(pcm(&original[..original.len() - removed]).is_err());
        }
        let mut duplicate = original.clone();
        chunk(&mut duplicate, b"data", &[0, 0]);
        let length = (duplicate.len() - 8) as u32;
        duplicate[4..8].copy_from_slice(&length.to_le_bytes());
        assert!(pcm(&duplicate).is_err());
    }
}

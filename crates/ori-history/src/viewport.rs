//! A bounded display projection, never a replacement for archived raw records.
use crate::{
    HistoryError, Result,
    storage::Roots,
    types::{MAX_RECORD_BYTES, MAX_RESULT_BYTES, valid_id},
};
use ori_archive::{Archive, MeasurementCursor, SweepQuery};
use ori_contracts::UtcTimestamp;
use ori_runtime::{qos::BackgroundQos, worker::Cancellation};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{collections::BTreeSet, io::Write, sync::Arc};

/// Full canonical JSON cursor bytes accepted by the existing HTTP transport.
/// This includes field names, UTF-8 and JSON escaping, not only the source ID.
pub const MAX_VIEWPORT_CURSOR_BYTES: usize = 4096;

struct CursorBudget {
    remaining: usize,
    exceeded: bool,
}
impl Write for CursorBudget {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        let Some(remaining) = self.remaining.checked_sub(bytes.len()) else {
            self.exceeded = true;
            return Err(std::io::Error::other("viewport cursor byte budget"));
        };
        self.remaining = remaining;
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ViewportCursor {
    pub boundary: u64,
    pub query_key: String,
    pub after: MeasurementCursor,
}
impl ViewportCursor {
    fn validate(&self) -> Result<()> {
        // A necessary lower bound avoids scanning arbitrarily large caller
        // strings. This is not a separate raw-ID policy: JSON cannot be shorter.
        if self
            .query_key
            .len()
            .checked_add(self.after.started_at.len())
            .and_then(|bytes| bytes.checked_add(self.after.id.len()))
            .is_none_or(|bytes| bytes > MAX_VIEWPORT_CURSOR_BYTES)
        {
            return Err(HistoryError::Invalid("viewport cursor byte budget"));
        }
        let mut budget = CursorBudget {
            remaining: MAX_VIEWPORT_CURSOR_BYTES,
            exceeded: false,
        };
        if serde_json::to_writer(&mut budget, self).is_err() {
            return Err(if budget.exceeded {
                HistoryError::Invalid("viewport cursor byte budget")
            } else {
                HistoryError::Internal
            });
        }
        if !hex_key(&self.query_key)
            || self.after.id.trim().is_empty()
            || self.after.started_at.parse::<UtcTimestamp>().is_err()
        {
            return Err(HistoryError::Invalid("viewport cursor"));
        }
        Ok(())
    }
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ViewportQuery {
    pub session_id: String,
    pub stream_id: String,
    pub min_frequency_hz: i64,
    pub max_frequency_hz: i64,
    pub columns: u16,
    pub rows: u16,
    #[serde(default)]
    pub start_us: Option<i64>,
    #[serde(default)]
    pub end_us: Option<i64>,
    #[serde(default)]
    pub cursor: Option<ViewportCursor>,
}
impl ViewportQuery {
    pub fn new(
        session_id: impl Into<String>,
        stream_id: impl Into<String>,
        minimum: i64,
        maximum: i64,
        columns: u16,
        rows: u16,
    ) -> Self {
        Self {
            session_id: session_id.into(),
            stream_id: stream_id.into(),
            min_frequency_hz: minimum,
            max_frequency_hz: maximum,
            columns,
            rows,
            start_us: None,
            end_us: None,
            cursor: None,
        }
    }
    pub fn validate(&self) -> Result<()> {
        if !valid_id(&self.session_id, 1024)
            || !valid_id(&self.stream_id, 1024)
            || self.min_frequency_hz < 0
            || self.max_frequency_hz < self.min_frequency_hz
            || !(1..=1024).contains(&self.columns)
            || !(1..=100).contains(&self.rows)
            || u32::from(self.columns) * u32::from(self.rows) > 32768
            || matches!((self.start_us,self.end_us),(Some(a),Some(b)) if a>=b)
        {
            return Err(HistoryError::Invalid(
                "viewport scope, range or cell budget",
            ));
        }
        if let Some(cursor) = &self.cursor {
            cursor.validate()?;
        }
        Ok(())
    }
}
#[derive(Clone, Serialize, Deserialize)]
pub struct ViewportCell {
    pub bucket: u16,
    pub frequency_hz: i64,
    pub level: f64,
    pub point_index: u32,
}
#[derive(Clone, Serialize, Deserialize)]
pub struct ViewportRow {
    pub measurement_id: String,
    pub started_at_us: i64,
    pub ended_at_us: i64,
    pub level_unit: String,
    pub settings_sha256: Option<String>,
    pub cells: Vec<ViewportCell>,
}
#[derive(Clone, Default, Serialize, Deserialize)]
pub struct ViewportQuality {
    pub incomplete_settings: bool,
    pub heterogeneous: bool,
    pub frequency_gaps: bool,
    pub paged: bool,
}
#[derive(Clone, Serialize, Deserialize)]
pub struct ViewportPage {
    pub derived: bool,
    pub boundary: u64,
    pub cache_key: String,
    pub rows: Vec<ViewportRow>,
    pub quality: ViewportQuality,
    pub next_cursor: Option<ViewportCursor>,
}
fn replayable(page: &ViewportPage) -> Result<()> {
    if let Some(cursor) = &page.next_cursor {
        // Never publish an unusable derived result, including a cached result
        // produced by an older version. Canonical source records stay intact.
        cursor.validate().map_err(|error| match error {
            HistoryError::Invalid(_) => HistoryError::ResourceLimit,
            other => other,
        })?;
    }
    Ok(())
}
pub(crate) fn hash(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|v| format!("{v:02x}"))
        .collect()
}
pub(crate) fn hex_key(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn json_bytes(value: &impl Serialize) -> Result<Vec<u8>> {
    ori_contracts::to_json_vec(value).map_err(|_| HistoryError::Internal)
}
fn time(value: &Value) -> Result<i64> {
    value
        .as_str()
        .ok_or(HistoryError::Internal)?
        .parse::<UtcTimestamp>()
        .map(|v| v.unix_micros())
        .map_err(|_| HistoryError::Internal)
}
fn filter_time(value: Option<i64>) -> Result<Option<String>> {
    value
        .map(|v| {
            chrono::DateTime::<chrono::Utc>::from_timestamp_micros(v)
                .map(|stamp| stamp.to_rfc3339_opts(chrono::SecondsFormat::Micros, true))
                .ok_or(HistoryError::Invalid("viewport UTC range"))
        })
        .transpose()
}
fn row(source: Value, query: &ViewportQuery) -> Result<(ViewportRow, String, bool)> {
    let frequencies = source["frequencies_hz"]
        .as_array()
        .ok_or(HistoryError::Internal)?;
    let levels = source["levels"].as_array().ok_or(HistoryError::Internal)?;
    if frequencies.is_empty() || frequencies.len() != levels.len() || frequencies.len() > 65536 {
        return Err(HistoryError::ResourceLimit);
    }
    let unit = source["level_unit"]
        .as_str()
        .ok_or(HistoryError::Internal)?
        .to_owned();
    let settings = source
        .get("settings")
        .filter(|v| v.as_object().is_some_and(|m| !m.is_empty()));
    let settings_sha256 = settings
        .map(json_bytes)
        .transpose()?
        .map(|bytes| hash(&bytes));
    let profile = hash(&json_bytes(
        &json!({"unit":unit,"settings":settings_sha256,"grid":hash(&json_bytes(frequencies)?)}),
    )?);
    let mut buckets: Vec<Option<ViewportCell>> = vec![None; usize::from(query.columns)];
    let minimum = query.min_frequency_hz as u128;
    let span = query.max_frequency_hz as u128 - minimum + 1;
    let mut previous = None;
    for (index, (frequency, level)) in frequencies.iter().zip(levels).enumerate() {
        let frequency = frequency
            .as_i64()
            .filter(|v| *v >= 0)
            .ok_or(HistoryError::Internal)?;
        let level = level
            .as_f64()
            .filter(|v| v.is_finite())
            .ok_or(HistoryError::Internal)?;
        if previous.is_some_and(|v| v > frequency) {
            return Err(HistoryError::Internal);
        }
        previous = Some(frequency);
        if !(query.min_frequency_hz..=query.max_frequency_hz).contains(&frequency) {
            continue;
        }
        let bucket = ((frequency as u128 - minimum) * u128::from(query.columns) / span) as usize;
        // Strict greater-than preserves the FIRST source point on equal maxima,
        // including the original sign of zero and the exact source frequency.
        if buckets[bucket]
            .as_ref()
            .is_none_or(|prior| level > prior.level)
        {
            buckets[bucket] = Some(ViewportCell {
                bucket: bucket as u16,
                frequency_hz: frequency,
                level,
                point_index: index as u32,
            });
        }
    }
    let cells = buckets.into_iter().flatten().collect();
    Ok((
        ViewportRow {
            measurement_id: source["id"].as_str().ok_or(HistoryError::Internal)?.into(),
            started_at_us: time(&source["interval"]["started_at"])?,
            ended_at_us: time(&source["interval"]["ended_at"])?,
            level_unit: unit,
            settings_sha256,
            cells,
        },
        profile,
        settings.is_none(),
    ))
}

pub(crate) async fn build(
    archive: Archive,
    roots: Arc<Roots>,
    nonce: String,
    query: ViewportQuery,
    qos: BackgroundQos,
    cancel: Cancellation,
) -> Result<ViewportPage> {
    query.validate()?;
    if cancel.is_cancelled() {
        return Err(HistoryError::Cancelled);
    }
    let boundary = match &query.cursor {
        Some(c) => c.boundary,
        None => archive.sweep_ingestion_boundary(&query.session_id).await?,
    };
    let context = json!({"source_instance":nonce,"boundary":boundary,"session_id":query.session_id,"stream_id":query.stream_id,
        "min_frequency_hz":query.min_frequency_hz,"max_frequency_hz":query.max_frequency_hz,"columns":query.columns,
        "rows":query.rows,"start_us":query.start_us,"end_us":query.end_us});
    let query_key = hash(&json_bytes(&context)?);
    if query
        .cursor
        .as_ref()
        .is_some_and(|c| c.query_key != query_key)
    {
        return Err(HistoryError::Invalid(
            "viewport cursor belongs to another immutable query",
        ));
    }
    let key = hash(&json_bytes(
        &json!({"query":query_key,"after":query.cursor.as_ref().map(|c|&c.after)}),
    )?);
    let cached = {
        let roots = roots.clone();
        let key = key.clone();
        tokio::task::spawn_blocking(move || roots.view(&key))
            .await
            .map_err(|_| HistoryError::Internal)??
    };
    if let Some(bytes) = cached {
        qos.account(bytes.len() as u64, &cancel).await?;
        let page: ViewportPage =
            serde_json::from_slice(&bytes).map_err(|_| HistoryError::Internal)?;
        if page.cache_key != key
            || page.boundary != boundary
            || !page.derived
            || page.rows.len() > usize::from(query.rows)
        {
            return Err(HistoryError::Internal);
        }
        replayable(&page)?;
        return Ok(page);
    }
    if archive.get_session(&query.session_id).await?.is_none() {
        return Err(HistoryError::NotFound);
    }
    let mut source_query = SweepQuery::session(&query.session_id);
    source_query.stream_id = Some(query.stream_id.clone());
    source_query.min_hz = Some(query.min_frequency_hz);
    source_query.max_hz = Some(query.max_frequency_hz);
    source_query.start = filter_time(query.start_us)?;
    source_query.end = filter_time(query.end_us)?;
    source_query.after = query.cursor.as_ref().map(|c| c.after.clone());
    source_query.limit = usize::from(query.rows);
    let selected = archive.sweep_summaries(source_query, boundary).await?;
    let mut rows = Vec::new();
    let mut profiles = BTreeSet::new();
    let mut quality = ViewportQuality::default();
    for summary in selected.items {
        if cancel.is_cancelled() {
            return Err(HistoryError::Cancelled);
        }
        let id = summary["id"].as_str().ok_or(HistoryError::Internal)?;
        let source = archive.get_sweep(id).await?.ok_or(HistoryError::Internal)?;
        let bytes = json_bytes(&source)?;
        if bytes.len() > MAX_RECORD_BYTES {
            return Err(HistoryError::ResourceLimit);
        }
        qos.account(bytes.len() as u64, &cancel).await?;
        let row_query = query.clone();
        let (value, profile, incomplete) =
            tokio::task::spawn_blocking(move || row(source, &row_query))
                .await
                .map_err(|_| HistoryError::Internal)??;
        quality.frequency_gaps |= value.cells.len() < usize::from(query.columns);
        quality.incomplete_settings |= incomplete;
        profiles.insert(profile);
        rows.push(value);
    }
    rows.sort_by(|a, b| {
        a.started_at_us
            .cmp(&b.started_at_us)
            .then_with(|| a.measurement_id.as_bytes().cmp(b.measurement_id.as_bytes()))
    });
    quality.heterogeneous = profiles.len() > 1;
    quality.paged = selected.next_cursor.is_some();
    let page = ViewportPage {
        derived: true,
        boundary,
        cache_key: key.clone(),
        rows,
        quality,
        next_cursor: selected.next_cursor.map(|after| ViewportCursor {
            boundary,
            query_key,
            after,
        }),
    };
    replayable(&page)?;
    let bytes = json_bytes(&page)?;
    if bytes.len() > MAX_RESULT_BYTES {
        return Err(HistoryError::ResourceLimit);
    }
    qos.account(bytes.len() as u64, &cancel).await?;
    tokio::task::spawn_blocking(move || roots.write_view(&key, &bytes))
        .await
        .map_err(|_| HistoryError::Internal)??;
    Ok(page)
}

use crate::{GuardError, GuardService, Result, util::*};
use base64::{Engine as _, engine::general_purpose::URL_SAFE};
use ori_archive::{MeasurementCursor, SweepQuery};
use ori_contracts::Sweep;
use serde_json::{Value, json};

#[derive(Clone, Debug)]
pub struct HistoryQuery {
    pub limit: usize,
    pub after: Option<String>,
    pub start: Option<String>,
    pub end: Option<String>,
    pub min_hz: u64,
    pub max_hz: u64,
    pub event_id: Option<String>,
}
impl Default for HistoryQuery {
    fn default() -> Self {
        Self {
            limit: 32,
            after: None,
            start: None,
            end: None,
            min_hz: 0,
            max_hz: 20_000_000_000,
            event_id: None,
        }
    }
}
pub(crate) fn cursor_encode(time: &str, id: &str) -> Result<String> {
    Ok(URL_SAFE.encode(spaced_json(&json!([time, id]), true)?))
}
pub(crate) fn cursor_decode(raw: &str) -> Result<MeasurementCursor> {
    if raw.len() > 16_384 {
        return Err(invalid("Неверный курсор истории/тревог"));
    }
    let bytes = URL_SAFE
        .decode(raw)
        .map_err(|_| invalid("Неверный курсор истории/тревог"))?;
    let value = ori_contracts::parse_json_value(&bytes, 8192)
        .map_err(|_| invalid("Неверный курсор истории/тревог"))?;
    let values = value
        .as_array()
        .filter(|a| a.len() == 2 && a.iter().all(Value::is_string))
        .ok_or_else(|| invalid("Неверный курсор истории/тревог"))?;
    let time = values[0]
        .as_str()
        .ok_or_else(|| invalid("cursor timestamp"))?;
    timestamp(time)?;
    Ok(MeasurementCursor {
        started_at: time.into(),
        id: values[1]
            .as_str()
            .ok_or_else(|| invalid("cursor id"))?
            .into(),
    })
}
fn selected(frequencies: &[u64], levels: &[f64], minimum: u64, maximum: u64) -> Vec<usize> {
    let indexes = frequencies
        .iter()
        .enumerate()
        .filter(|(_, hz)| **hz >= minimum && **hz <= maximum)
        .map(|(i, _)| i)
        .collect::<Vec<_>>();
    if indexes.len() <= 320 {
        return indexes;
    }
    let bucket = indexes.len().div_ceil(320);
    indexes
        .chunks(bucket)
        .map(|part| {
            let mut best = part[0];
            for &i in &part[1..] {
                if levels[i] > levels[best] {
                    best = i;
                }
            }
            best
        })
        .collect()
}
impl GuardService {
    pub async fn history(&self, id: &str, query: HistoryQuery) -> Result<Value> {
        self.project(id).await?;
        if !(1..=64).contains(&query.limit)
            || query.min_hz > query.max_hz
            || query.max_hz > 20_000_000_000
        {
            return Err(invalid("Неверные фильтры истории"));
        }
        if let Some(event) = query.event_id.as_deref().filter(|s| !s.is_empty()) {
            self.event_record(id, event).await?;
        }
        let start = query
            .start
            .as_deref()
            .map(timestamp)
            .transpose()?
            .map(|t| t.as_str().to_owned());
        let end = query
            .end
            .as_deref()
            .map(timestamp)
            .transpose()?
            .map(|t| t.as_str().to_owned());
        if matches!((&start,&end),(Some(a),Some(b)) if a>b) {
            return Err(invalid("Начало позже окончания"));
        }
        let after = query
            .after
            .as_deref()
            .filter(|s| !s.is_empty())
            .map(cursor_decode)
            .transpose()?;
        let page = self
            .archive
            .sweep_page(SweepQuery {
                session_id: None,
                stream_id: None,
                project_id: Some(id.into()),
                event_id: query.event_id.filter(|s| !s.is_empty()),
                start,
                end,
                min_hz: Some(query.min_hz as i64),
                max_hz: Some(query.max_hz as i64),
                after,
                limit: query.limit,
            })
            .await?;
        let mut items = Vec::with_capacity(page.items.len());
        for mut raw in page.items {
            let sweep: Sweep = decode(&raw)?;
            let indexes = selected(
                &sweep.frequencies_hz,
                &sweep.levels,
                query.min_hz,
                query.max_hz,
            );
            raw["frequencies_hz"] = json!(
                indexes
                    .iter()
                    .map(|i| sweep.frequencies_hz[*i])
                    .collect::<Vec<_>>()
            );
            raw["levels"] = json!(indexes.iter().map(|i| sweep.levels[*i]).collect::<Vec<_>>());
            raw["original_point_count"] = json!(sweep.levels.len());
            raw["display_only"] = json!(true);
            raw["settings"] = Value::Object(
                raw["settings"]
                    .as_object()
                    .ok_or_else(|| invalid("sweep settings"))?
                    .iter()
                    .filter(|(k, v)| {
                        [
                            "adapter",
                            "rbw_hz",
                            "rbw_requested_hz",
                            "receive_profile",
                            "generator_output",
                            "overload",
                            "incomplete",
                            "simulated",
                            "scan_band_id",
                            "scan_plan_id",
                            "scan_plan_revision",
                        ]
                        .contains(&k.as_str())
                            && (v.is_null()
                                || v.is_number()
                                || v.is_boolean()
                                || v.as_str().is_some_and(|s| s.chars().count() <= 200))
                    })
                    .map(|(k, v)| (k.clone(), v.clone()))
                    .collect(),
            );
            raw["settings_available_in_original"] = json!(true);
            items.push(raw);
        }
        let next = page
            .next_cursor
            .map(|c| cursor_encode(&c.started_at, &c.id))
            .transpose()?;
        Ok(json!({"items":items,"next_cursor":next}))
    }
    pub async fn history_original(&self, id: &str, sweep_id: &str) -> Result<Value> {
        self.project(id).await?;
        let sweep = self
            .archive
            .get_sweep(sweep_id)
            .await?
            .ok_or(GuardError::NotFoundContext(
                "Свип другого проекта/отсутствует",
            ))?;
        if !self
            .archive
            .monitoring_member(id, s(&sweep, "session_id")?)
            .await?
        {
            return Err(GuardError::NotFoundContext(
                "Свип другого проекта/отсутствует",
            ));
        }
        Ok(sweep)
    }
    pub async fn history_comparison(&self, id: &str, sweep_id: &str) -> Result<Value> {
        let project = self.project(id).await?;
        let raw = self.history_original(id, sweep_id).await?;
        let baseline = if let (Some(plan), Some(band)) = (
            raw["settings"]["scan_plan_id"].as_str(),
            raw["settings"]["scan_band_id"].as_str(),
        ) {
            self.archive
                .latest_scan_baseline(id, plan, band)
                .await?
                .ok_or(GuardError::NotFound)?
        } else {
            self.get_record("baseline", s(&project, "baseline_id")?, Some(id))
                .await?
        };
        let sweep: Sweep = decode(&raw)?;
        let compatible = ori_monitoring::fingerprint(&sweep)? == s(&baseline, "fingerprint")?;
        let mut items = vec![];
        if compatible {
            let positions = sweep
                .frequencies_hz
                .iter()
                .enumerate()
                .map(|(i, hz)| (*hz, i))
                .collect::<std::collections::BTreeMap<_, _>>();
            for index in selected(&sweep.frequencies_hz, &sweep.levels, 0, u64::MAX) {
                let hz = sweep.frequencies_hz[index];
                let bin = positions[&hz];
                let p50 = baseline["p50_dbm"]
                    .as_array()
                    .and_then(|a| a.get(bin))
                    .and_then(Value::as_f64)
                    .ok_or_else(|| invalid("baseline p50"))?;
                let p95 = baseline["p95_dbm"]
                    .as_array()
                    .and_then(|a| a.get(bin))
                    .and_then(Value::as_f64)
                    .ok_or_else(|| invalid("baseline p95"))?;
                items.push(json!({"frequency_hz":hz,"baseline_p50_dbm":p50,"baseline_p95_dbm":p95,"current_dbm":sweep.levels[index],"delta_db":sweep.levels[index]-p50}));
            }
        }
        Ok(
            json!({"baseline_id":baseline["id"],"sweep_id":sweep_id,"detector_version":baseline["detector_version"],"compatible":compatible,"reason":if compatible{None}else{Some("Сетка/настройки не совпадают с эталоном")},"items":items,"display_only":true}),
        )
    }
}

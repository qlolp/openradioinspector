use crate::{GuardService, Result, util::*};
use ori_archive::{KnownSignalTarget, MonitoringQuery};
use ori_contracts::UtcTimestamp;
use serde::Deserialize;
use serde_json::{Value, json};
use std::{collections::BTreeMap, sync::LazyLock};

const MAX_FREQUENCY_HZ: u64 = 20_000_000_000;

#[derive(Deserialize)]
struct BandPlan {
    profile: String,
    version: u64,
    items: Vec<Value>,
}

static BAND_PLAN: LazyLock<BandPlan> = LazyLock::new(|| {
    serde_json::from_str(include_str!("band-plan-ru-v1.json")).expect("checked band plan")
});

pub fn band_plan(start_hz: u64, end_hz: u64) -> Result<Value> {
    if start_hz > end_hz || end_hz > MAX_FREQUENCY_HZ {
        return Err(invalid("Нужны границы диапазона в целых Гц по возрастанию"));
    }
    Ok(json!({
        "profile": BAND_PLAN.profile,
        "version": BAND_PLAN.version,
        "items": overlapping_bands(start_hz, end_hz)
    }))
}

fn overlapping_bands(start_hz: u64, end_hz: u64) -> Vec<&'static Value> {
    BAND_PLAN
        .items
        .iter()
        .filter(|band| {
            band["start_hz"]
                .as_u64()
                .is_some_and(|start| start <= end_hz)
                && band["end_hz"].as_u64().is_some_and(|end| end >= start_hz)
        })
        .collect()
}

fn kind_label(kind: &str) -> Option<&'static str> {
    Some(match kind {
        "broadcast" => "Радио или телевещание",
        "aviation" => "Авиационная связь",
        "maritime" => "Морская связь",
        "rail" => "Железнодорожная связь",
        "municipal" => "Городские службы",
        "cellular" => "Мобильная сеть",
        "short_range" => "Устройство малого радиуса действия",
        "wifi" => "Wi-Fi",
        "bluetooth" => "Bluetooth",
        "remote_control" => "Радиоуправление",
        "satellite_navigation" => "Спутниковая навигация",
        "other" => "Другой тип",
        "unknown" => "Не опознан",
        _ => return None,
    })
}

fn signal_target(item: &Value) -> Option<KnownSignalTarget<'_>> {
    let center_frequency_hz = item["center_frequency_hz"].as_u64()?;
    let bandwidth_hz = detected_width(item)?;
    (center_frequency_hz <= MAX_FREQUENCY_HZ && bandwidth_hz <= MAX_FREQUENCY_HZ).then_some(
        KnownSignalTarget {
            center_frequency_hz,
            scan_plan_id: item["scan_plan_id"].as_str(),
            scan_band_id: item["scan_band_id"].as_str(),
            scan_plan_revision: item["scan_plan_revision"].as_u64(),
            bandwidth_hz,
            bin_span: bin_span(item),
        },
    )
}

fn bin_span(item: &Value) -> Option<u64> {
    item["bin_span"].as_u64().or_else(|| {
        item["last_bin"]
            .as_u64()?
            .checked_sub(item["first_bin"].as_u64()?)
    })
}

fn detected_width(event: &Value) -> Option<u64> {
    event["end_frequency_hz"]
        .as_u64()?
        .checked_sub(event["start_frequency_hz"].as_u64()?)
}

fn observation_features(event: &Value) -> Value {
    let analysis = &event["analysis"];
    let evidence = &analysis["verification_evidence"];
    let receiver = &evidence["receiver"];
    let confirmed_mode = (analysis["verification"] == "observed_both"
        && evidence["simulated"] == false
        && receiver["decoder_source"] == "receiver_RX")
        .then(|| {
            receiver["decoded_mode"]
                .as_str()
                .filter(|mode| !mode.is_empty())
        })
        .flatten();
    json!({
        "detected_width_hz": detected_width(event),
        "source_sweep_count": event["source_count"].as_u64(),
        "confirmed_receiver_mode": confirmed_mode
    })
}

fn recognition(event: &Value, known: &[Value], detailed: bool) -> Value {
    let target = signal_target(event);
    let matches: Vec<_> = known
        .iter()
        .filter(|entry| {
            target.zip(signal_target(entry)).is_some_and(|(a, b)| {
                a.center_frequency_hz == b.center_frequency_hz
                    && a.scan_plan_id == b.scan_plan_id
                    && a.scan_band_id == b.scan_band_id
                    && a.scan_plan_revision == b.scan_plan_revision
                    && (a.bandwidth_hz.max(b.bandwidth_hz)
                        <= 2 * a.bandwidth_hz.min(b.bandwidth_hz)
                        || (a.bandwidth_hz == 0 && b.bin_span == Some(1))
                        || (b.bandwidth_hz == 0 && a.bin_span == Some(1)))
            })
        })
        .collect();
    let state = if event["kind"] == "authorized_changed" {
        "changed"
    } else if matches.is_empty() {
        "new"
    } else {
        "similar"
    };
    if !detailed {
        return json!({"state": state});
    }

    let bands = target.map_or_else(Vec::new, |target| {
        overlapping_bands(target.center_frequency_hz, target.center_frequency_hz)
    });
    let mut hypotheses = BTreeMap::new();
    for band in &bands {
        for kind in band["kinds"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
        {
            if let Some(label) = kind_label(kind) {
                hypotheses.entry(kind).or_insert_with(|| json!({
                    "kind": kind, "label": label,
                    "reason": format!("Совпадение со справочной полосой «{}»", band["label"].as_str().unwrap_or(""))
                }));
            }
        }
    }
    let known_matches = matches
        .into_iter()
        .map(|entry| {
            json!({
                "id": entry["id"], "name": entry["name"], "kind": entry["kind"],
                "reason": "Совпали центральная частота, профиль обзора и близкая измеренная ширина"
            })
        })
        .collect::<Vec<_>>();
    json!({
        "state": state,
        "band_matches": bands,
        "known_matches": known_matches,
        "hypotheses": hypotheses.into_values().collect::<Vec<_>>(),
        "observed_features": observation_features(event)
    })
}

impl GuardService {
    pub async fn recognized_events(&self, id: &str, after: &str, limit: usize) -> Result<Value> {
        if !(1..=200).contains(&limit) {
            return Err(invalid("Допустимо от 1 до 200 событий на страницу"));
        }
        let mut page = self
            .list_records("event", Some(id), after, limit, true)
            .await?;
        let items = page["items"]
            .as_array_mut()
            .ok_or_else(|| invalid("Список событий"))?;
        let targets: Vec<_> = items.iter().filter_map(signal_target).collect();
        let known = self.archive.matching_known_signals(id, &targets).await?;
        for event in items {
            event["recognition"] = recognition(event, &known, false);
        }
        Ok(page)
    }

    pub async fn event_with_recognition(&self, id: &str, event_id: &str) -> Result<Value> {
        let mut event = self.get_record("event", event_id, Some(id)).await?;
        let targets: Vec<_> = signal_target(&event).into_iter().collect();
        let known = self.archive.matching_known_signals(id, &targets).await?;
        event["recognition"] = recognition(&event, &known, true);
        if let Some(peak) = self.archive.monitoring("event-peak", event_id).await?
            && peak["project_id"] == id
            && peak["center_frequency_hz"] == event["center_frequency_hz"]
            && peak["peak_level"] == event["peak_level"]
        {
            event["peak_source"] = json!({"session_id": peak["session_id"], "stream_id": peak["stream_id"], "sweep_id": peak["sweep_id"]});
        }
        Ok(event)
    }

    pub async fn known_signals(&self, id: &str, before: &str, limit: usize) -> Result<Value> {
        self.project(id).await?;
        if !(1..=100).contains(&limit) {
            return Err(invalid("Допустимо от 1 до 100 сигналов на страницу"));
        }
        if !before.is_empty() {
            self.get_record("known-signal", before, Some(id)).await?;
        }
        let mut items = self
            .archive
            .list_monitoring(
                "known-signal",
                MonitoringQuery {
                    project_id: Some(id.to_owned()),
                    after: before.to_owned(),
                    recent: true,
                    limit: limit + 1,
                    ..Default::default()
                },
            )
            .await?;
        let next = if items.len() > limit {
            items.truncate(limit);
            items
                .last()
                .map(|item| item["id"].clone())
                .unwrap_or(Value::Null)
        } else {
            Value::Null
        };
        Ok(json!({"items": items, "next_cursor": next}))
    }

    async fn catalog_peak_source(&self, project: &str, event: &Value) -> Result<Value> {
        let event_id = s(event, "id")?;
        let center = integer(event, "center_frequency_hz")?;
        let peak = number(event, "peak_level")?;
        let first_seen = timestamp(s(event, "first_seen")?)?;
        let last_seen = timestamp(s(event, "last_seen")?)?;
        if first_seen > last_seen {
            return Err(invalid("Неверный интервал исходного сигнала"));
        }
        let stored = self.archive.monitoring("event-peak", event_id).await?;
        let mut refs: Vec<_> = stored
            .into_iter()
            .filter(|source| source["project_id"] == project)
            .chain(
                event["source_refs"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .rev()
                    .cloned(),
            )
            .collect();
        let mut cursor = Some(String::new());
        loop {
            for reference in &refs {
                let (Ok(session_id), Ok(stream_id), Ok(sweep_id)) = (
                    s(reference, "session_id"),
                    s(reference, "stream_id"),
                    s(reference, "sweep_id"),
                ) else {
                    continue;
                };
                if event["run_session_id"]
                    .as_str()
                    .is_some_and(|id| id != session_id)
                {
                    continue;
                }
                if let Some(sweep) = self.archive.get_sweep(sweep_id).await?
                    && sweep["session_id"] == session_id
                    && sweep["stream_id"] == stream_id
                    && sweep["level_unit"] == "dBm"
                    && sweep["settings"]["scan_plan_id"] == event["scan_plan_id"]
                    && sweep["settings"]["scan_band_id"] == event["scan_band_id"]
                    && sweep["settings"]["scan_plan_revision"] == event["scan_plan_revision"]
                    && contains_catalog_peak(&sweep, center, peak, &first_seen, &last_seen)?
                    && self.archive.monitoring_member(project, session_id).await?
                {
                    return Ok(
                        json!({"session_id": session_id, "stream_id": stream_id, "sweep_id": sweep_id}),
                    );
                }
            }
            let Some(after) = cursor else {
                return Err(invalid("Исходный пик события недоступен"));
            };
            let page = self
                .archive
                .event_sources(project, event_id, &after, 64)
                .await?;
            refs = page.items;
            cursor = page.next_cursor;
        }
    }

    pub async fn save_known_signal(&self, id: &str, payload: Value) -> Result<Value> {
        fields(&payload, &["event_id", "name", "kind"], true)?;
        let event_id = s(&payload, "event_id")?;
        let name = text_with_label(&payload, "name", "Название сигнала", 200)?;
        let kind = s(&payload, "kind")?;
        kind_label(kind).ok_or_else(|| invalid("Неизвестный тип сигнала"))?;
        self.ensure_open()?;
        let event = self.get_record("event", event_id, Some(id)).await?;
        let center = integer(&event, "center_frequency_hz")?;
        let start = integer(&event, "start_frequency_hz")?;
        let end = integer(&event, "end_frequency_hz")?;
        if !(start..=end).contains(&center) || end > MAX_FREQUENCY_HZ {
            return Err(invalid("Неверные границы исходного сигнала"));
        }
        let source = self.catalog_peak_source(id, &event).await?;
        let _life = self.lock_project(id).await?;
        let _tx = self.coordinator.lock().await;
        self.ensure_open()?;
        let now = self.clock.now();
        let prior = self.archive.monitoring("known-signal", event_id).await?;
        let created_at = prior
            .as_ref()
            .and_then(|item| item["created_at"].as_str())
            .unwrap_or(now.as_str());
        let record = json!({
            "id": event_id, "event_id": event_id, "project_id": id, "schema_version": 1,
            "name": name, "kind": kind, "created_at": created_at, "confirmed_at": now.as_str(),
            "first_seen": event["first_seen"], "last_seen": event["last_seen"],
            "center_frequency_hz": center, "start_frequency_hz": start, "end_frequency_hz": end,
            "bandwidth_hz": end - start, "bin_span": bin_span(&event),
            "peak_level": event["peak_level"], "level_unit": "dBm",
            "scan_plan_id": event["scan_plan_id"], "scan_band_id": event["scan_band_id"],
            "scan_plan_revision": event["scan_plan_revision"],
            "baseline_id": event["baseline_id"], "source_refs": [source]
        });
        self.archive
            .put_monitoring("known-signal", record.clone())
            .await?;
        Ok(record)
    }
}

fn contains_catalog_peak(
    sweep: &Value,
    center: u64,
    peak: f64,
    first_seen: &UtcTimestamp,
    last_seen: &UtcTimestamp,
) -> Result<bool> {
    if timestamp(s(&sweep["interval"], "started_at")?)? < *first_seen
        || timestamp(s(&sweep["interval"], "ended_at")?)? > *last_seen
    {
        return Ok(false);
    }
    let Some(bin) = sweep["frequencies_hz"]
        .as_array()
        .and_then(|grid| grid.iter().position(|hz| hz.as_u64() == Some(center)))
    else {
        return Ok(false);
    };
    Ok(sweep["levels"][bin].as_f64() == Some(peak))
}

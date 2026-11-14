use crate::{
    DetectedRegion, DetectionRequest,
    state::{Context, Recent, error},
};
use ori_session::{SessionError, SessionResult, TuneCompletionCheck};
use serde_json::{Value, json};

fn unsigned(value: &Value) -> u64 {
    value.as_u64().unwrap_or(0)
}
fn duplicate(
    context: &Context,
    stream: &str,
    region: &DetectedRegion,
    existing: &[Value],
) -> SessionResult<bool> {
    let tolerance = unsigned(&context.policy["dedup_bandwidth_hz"]).max(region.bandwidth_hz);
    if existing.iter().any(|candidate| {
        candidate["source_stream_id"] == stream
            && ["new", "queued", "investigating", "dismissed"]
                .contains(&candidate["state"].as_str().unwrap_or(""))
            && unsigned(&candidate["center_frequency_hz"]).abs_diff(region.center_frequency_hz)
                <= tolerance.max(unsigned(&candidate["bandwidth_hz"]).max(1))
    }) {
        return Ok(true);
    }
    let now = (context.monotonic)();
    let mut state = context.current_state()?;
    state.recent.retain(|item| item.expires > now);
    Ok(state.recent.iter().any(|item| {
        item.stream == stream
            && item.frequency.abs_diff(region.center_frequency_hz) <= tolerance.max(item.bandwidth)
    }))
}
fn region_valid(region: &DetectedRegion, sweep: &Value) -> bool {
    region.bandwidth_hz > 0
        && region.bandwidth_hz <= i64::MAX as u64
        && region.center_frequency_hz <= i64::MAX as u64
        && region.peak_level.is_finite()
        && region.score.is_finite()
        && sweep["frequencies_hz"]
            .as_array()
            .is_some_and(|frequencies| {
                frequencies.iter().enumerate().any(|(index, hz)| {
                    hz.as_u64() == Some(region.center_frequency_hz)
                        && sweep["levels"][index]
                            .as_f64()
                            .is_some_and(|level| level.to_bits() == region.peak_level.to_bits())
                })
            })
}
pub(crate) async fn run(context: &Context, session: &Value) -> SessionResult<Value> {
    let after = context.current_state()?.cursor;
    let pending = context
        .archive
        .sweeps_after_ingestion(
            &context.entry.id,
            context.policy["stream_id"].as_str(),
            after,
            1000,
        )
        .await?;
    let mut created = Vec::new();
    let mut duplicates = 0;
    let mut processed = 0;
    let mut last = Value::Null;
    for (sequence, sweep) in &pending {
        context.check.revalidate()?;
        last = sweep["id"].clone();
        let stream = sweep["stream_id"]
            .as_str()
            .ok_or_else(|| error("automation_source_invalid", "Некорректный исходный поток"))?;
        let definition = session["streams"]
            .as_array()
            .and_then(|items| items.iter().find(|item| item["id"] == stream))
            .ok_or_else(|| {
                error(
                    "automation_source_invalid",
                    "Исходный поток отсутствует в сеансе",
                )
            })?;
        if definition["role"] != "spectrum_analyzer" || definition["level_unit"] != "dBm" {
            context.current_state()?.cursor = *sequence;
            continue;
        }
        if sweep["level_unit"] != "dBm" {
            return Err(SessionError::invalid(
                "Автоматическое обнаружение поддерживает только исходные dBm",
            ));
        }
        let maximum = unsigned(&context.policy["max_candidates_per_sweep"]) as u16;
        let regions = context
            .effects
            .detect(
                DetectionRequest {
                    sweep: sweep.clone(),
                    threshold_dbm: context.policy["threshold_dbm"].as_f64().unwrap(),
                    maximum,
                },
                context.check.clone(),
                context.check.cancel.clone(),
            )
            .await?;
        context.check.revalidate()?;
        if regions.len() > usize::from(maximum)
            || regions.iter().any(|region| !region_valid(region, sweep))
        {
            return Err(error(
                "automation_science_invalid",
                "Некорректный результат научного обработчика",
            ));
        }
        let mut existing = context
            .archive
            .session_candidates(&context.entry.id, None)
            .await?;
        for region in regions {
            context.check.revalidate()?;
            if duplicate(context, stream, &region, &existing)? {
                duplicates += 1;
                continue;
            }
            if context.current_state()?.recent.len() >= context.max_recent {
                return Err(error(
                    "automation_capacity",
                    "Лимит runtime-контрольных точек; исходные данные сохранены",
                ));
            }
            let value=context.sessions.create_candidate_with_check(&context.entry.id,json!({"source_stream_id":stream,"interval":sweep["interval"],
                "center_frequency_hz":region.center_frequency_hz,"bandwidth_hz":region.bandwidth_hz,"peak_level":region.peak_level,"score":region.score,"level_unit":"dBm"}),context.check.clone()).await?;
            existing.push(value.clone());
            created.push(value.clone());
            {
                let mut state = context.current_state()?;
                state.recent.push(Recent {
                    stream: stream.into(),
                    frequency: region.center_frequency_hz,
                    bandwidth: region.bandwidth_hz,
                    expires: (context.monotonic)()
                        + context.policy["cooldown_seconds"].as_f64().unwrap(),
                });
            }
            context.publish("automatic_detection.candidate_created",json!({"candidate_id":value["id"],"sweep_id":sweep["id"],"source_stream_id":stream,
                "center_frequency_hz":value["center_frequency_hz"],"peak_level":value["peak_level"],"level_unit":value["level_unit"]})).await?;
        }
        processed += 1;
        context.check.revalidate()?;
        context.current_state()?.cursor = *sequence;
    }
    context.check.revalidate()?;
    let mut state = context.current_state()?;
    if pending.len() == 1000 {
        state.next = (context.monotonic)();
    }
    for (key, amount) in [
        ("sweeps_processed", processed),
        ("candidates_created", created.len() as u64),
        ("duplicates_suppressed", duplicates),
    ] {
        state.runtime[key] = json!(unsigned(&state.runtime[key]).saturating_add(amount));
    }
    state.runtime["last_sweep_id"] = last;
    state.runtime["last_error"] = Value::Null;
    state.runtime["last_result"] = json!(if !created.is_empty() {
        "candidates_created"
    } else if pending.is_empty() {
        "no_new_sweeps"
    } else {
        "idle"
    });
    Ok(json!(created))
}

//! A cyclic receiver is its own primary source. References and rolling state
//! are paged by frequency; the project holds only the accepted reference ID
//! and its transactional ingestion cursor.
use crate::{GuardError, GuardService, Result, util::*};
use ori_archive::Mutation;
use ori_monitoring::counts::{CountReference, ardv1_plan_key};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};

pub(crate) const BLOCK_POINTS: usize = 64;
const BUILD: &str = "duty-reference-build";
pub(crate) const BLOCK: &str = "duty-reference-block";
const DETECTOR: &str = "receiver-point-detector";

pub(crate) fn plan(project: &Value) -> Result<Value> {
    let settings = &project["profile"]["receiver"]["settings"];
    let raw = &settings["fast_scan"];
    let start = crate::preflight::receiver_frequency(&raw["start_hz"])?;
    let stop = crate::preflight::receiver_frequency(&raw["stop_hz"])?;
    let step = integer(raw, "step_hz")?;
    let settle = raw
        .get("settle_s")
        .map(Value::as_f64)
        .unwrap_or(Some(0.015))
        .filter(|s| (0.0..=1.).contains(s))
        .ok_or_else(|| invalid("Выдержка AR-DV1"))?;
    if raw["cyclic"] != true
        || stop <= start
        || step == 0
        || !step.is_multiple_of(10)
        || settings["receiver_profile"] != "ardv1-monitor-v1"
        || settings.get("mode").and_then(Value::as_str).unwrap_or("FM") != "FM"
    {
        return Err(invalid("Нужен циклический RX-план AR-DV1 в FM"));
    }
    let points = (stop - start).div_ceil(step) + 1;
    if points > 129_991 {
        return Err(invalid("Слишком много точек плана AR-DV1"));
    }
    let key = ardv1_plan_key(&decode(&project["profile"]["receiver"])?)?;
    Ok(
        json!({"scan_start_hz":start,"scan_stop_hz":stop,"scan_step_hz":step,"settle_s":settle,"mode":"FM","points":points,"plan_key":key}),
    )
}

pub(crate) fn block_id(baseline: &str, index: usize) -> String {
    format!("{baseline}.{index:06}")
}

fn compatible_plan(expected: &Value, actual: &Value) -> bool {
    [
        "scan_start_hz",
        "scan_stop_hz",
        "scan_step_hz",
        "points",
        "mode",
        "plan_key",
    ]
    .iter()
    .all(|&k| expected[k] == actual[k])
        && expected["settle_s"].as_f64() == actual["settle_s"].as_f64()
}

impl GuardService {
    pub(crate) async fn receiver_collection(&self, p: &Value) -> Result<Value> {
        let Ok(expected) = plan(p) else {
            return Ok(
                json!({"ready":false,"points":0,"ready_points":0,"passes":0,"minimum_passes":20,"minimum_seconds":60}),
            );
        };
        let mut progress = self
            .archive
            .ardv1_reference_progress(s(p, "active_session_id")?, s(p, "receiver_stream_id")?)
            .await?;
        if progress["context_key"].is_null() || !compatible_plan(&expected, &progress) {
            progress["points"] = expected["points"].clone();
            progress["ready_points"] = json!(0);
            progress["minimum_passes_observed"] = json!(0);
            progress["ready"] = json!(false);
        }
        progress["passes"] = progress["minimum_passes_observed"].clone();
        progress["minimum_passes"] = json!(20);
        progress["minimum_seconds"] = json!(60);
        Ok(progress)
    }

    // A crash can leave a partially built reference. Resume the same immutable
    // source selection and expose its ID only after the last block is durable.
    pub(crate) async fn reference_build(&self, p: &Value, inputs: &Value) -> Result<Value> {
        let id = s(p, "id")?;
        if let Some(old) = self.archive.monitoring(BUILD, id).await?
            && old["session_id"] == p["active_session_id"]
            && old["complete"] != true
            && old["inputs"]["context_key"] == inputs["context_key"]
        {
            let snapshot_valid = if crate::rfe::survey(p) {
                let keys = serde_json::from_value::<Vec<i64>>(old["inputs"]["pass_keys"].clone())
                    .map_err(|_| invalid("RF Explorer reference keys"))?;
                self.archive
                    .rfe_guard_sources_valid(
                        s(p, "active_session_id")?,
                        s(p, "analyzer_stream_id")?,
                        &keys,
                        s(inputs, "context_key")?,
                        integer(&old["inputs"], "windows")? as usize,
                    )
                    .await?
            } else {
                true
            };
            if snapshot_valid {
                return Ok(old);
            }
        }
        let mut build=self.record(Some(id),json!({"session_id":p["active_session_id"],"inputs":inputs,"next_block":0,"complete":false}))?;
        build["baseline_id"] = build["id"].clone();
        build["id"] = json!(id);
        self.archive.put_monitoring(BUILD, build.clone()).await?;
        Ok(build)
    }

    pub(crate) async fn persist_reference_block(
        &self,
        build: &mut Value,
        index: usize,
        data: Value,
    ) -> Result<()> {
        self.ensure_open()?;
        let mut record = data;
        record["id"] = json!(block_id(s(build, "baseline_id")?, index));
        record["project_id"] = build["project_id"].clone();
        record["created_at"] = build["created_at"].clone();
        build["next_block"] = json!(index + 1);
        self.archive
            .apply(vec![
                Self::mutation(BLOCK, record),
                Self::mutation(BUILD, build.clone()),
            ])
            .await?;
        Ok(())
    }

    pub(crate) async fn publish_reference(
        &self,
        p: &Value,
        payload: &Value,
        mut build: Value,
        mut reference: Value,
    ) -> Result<Value> {
        let id = s(p, "id")?;
        reference["id"] = build["baseline_id"].clone();
        reference["project_id"] = p["id"].clone();
        reference["created_at"] = build["created_at"].clone();
        reference["name"] = json!(text(payload, "name", 200)?);
        reference["location"] = json!(text(payload, "location", 200)?);
        reference["confirmed"] = json!(true);
        reference["preparation"] = p["preparation"].clone();
        reference["primary_source_kind"] = p["primary_source_kind"].clone();
        reference["session_id"] = p["active_session_id"].clone();
        reference["stream_id"] = p["primary_stream_id"].clone();
        reference["block_count"] = build["next_block"].clone();
        if build["source_count"].is_number() {
            reference["source_count"] = build["source_count"].clone();
        }
        let _tx = self.coordinator.lock().await;
        self.ensure_open()?;
        let mut current = self.project(id).await?;
        if current["active_session_id"] != p["active_session_id"]
            || self.runtime_snapshot(id)?.is_some()
        {
            return Err(GuardError::Conflict(
                "Источник сменился во время подготовки эталона".into(),
            ));
        }
        current["state"] = json!("ready");
        current["baseline_id"] = reference["id"].clone();
        if reference["windows"].is_number() {
            current["rfe_reference_windows"] = reference["windows"].clone();
        }
        current["detector_rowid"] = reference["boundary"].clone();
        current["detector_cursor"] = Value::Null;
        reset_detectors(&mut current);
        build["complete"] = json!(true);
        self.archive
            .apply(vec![
                Self::mutation("baseline", reference.clone()),
                Self::mutation(BUILD, build),
                Self::mutation("project", current),
            ])
            .await?;
        Ok(reference)
    }

    pub(crate) async fn receiver_reference(&self, p: &Value, payload: &Value) -> Result<Value> {
        let progress = self.receiver_collection(p).await?;
        if progress["ready"] != true {
            return Err(invalid(
                "Каждой точке AR-DV1 нужны 20 совместимых проходов за 60 секунд",
            ));
        }
        let mut build = self.reference_build(p, &progress).await?;
        let input = build["inputs"].clone();
        let points = integer(&input, "points")? as usize;
        let pages = points.div_ceil(BLOCK_POINTS);
        let mut next = integer(&build, "next_block")? as usize;
        while next < pages {
            let first = next * BLOCK_POINTS;
            let count = (points - first).min(BLOCK_POINTS);
            let rows = self
                .archive
                .ardv1_reference_points(
                    s(p, "active_session_id")?,
                    s(p, "receiver_stream_id")?,
                    s(&input, "context_key")?,
                    integer(&input, "boundary")?,
                    first,
                    count,
                )
                .await?;
            if rows.len() != count {
                return Err(invalid("Неполная сетка эталона AR-DV1"));
            }
            let mut references = Vec::with_capacity(count);
            for (i, row) in rows.iter().enumerate() {
                let samples = row["samples"]
                    .as_array()
                    .ok_or_else(|| invalid("Исходные наблюдения эталона"))?;
                let passes = samples
                    .iter()
                    .map(|v| integer(v, "pass"))
                    .collect::<Result<BTreeSet<_>>>()?;
                let start = samples
                    .iter()
                    .map(|v| s(v, "started_at"))
                    .collect::<Result<Vec<_>>>()?
                    .into_iter()
                    .min();
                let end = samples
                    .iter()
                    .map(|v| s(v, "ended_at"))
                    .collect::<Result<Vec<_>>>()?
                    .into_iter()
                    .max();
                let independent = samples.windows(2).all(|pair| {
                    let previous = &pair[0];
                    let next = &pair[1];
                    previous["started_at"].as_str() < next["started_at"].as_str()
                        && previous["ended_at"].as_str() <= next["started_at"].as_str()
                });
                if !independent
                    || row["index"] != first + i
                    || passes.len() != samples.len()
                    || passes.len() < 20
                    || start
                        .zip(end)
                        .map(|(a, b)| elapsed(a, b))
                        .transpose()?
                        .is_none_or(|v| v < 60.)
                {
                    return Err(invalid("Недостаточно независимых исходных проходов точки"));
                }
                let counts = samples
                    .iter()
                    .map(|v| raw_count(&v["level"]))
                    .collect::<Result<Vec<_>>>()?;
                let mut reference = value(&CountReference::from_counts(&counts)?)?;
                reference["frequency_hz"] = row["frequency_hz"].clone();
                reference["observation_ids"] = json!(
                    samples
                        .iter()
                        .map(|s| &s["observation_id"])
                        .collect::<Vec<_>>()
                );
                references.push(reference);
            }
            let samples = references
                .iter()
                .filter_map(|p| p["observation_ids"].as_array())
                .map(Vec::len)
                .sum::<usize>();
            build["source_count"] =
                json!(build["source_count"].as_u64().unwrap_or(0) + samples as u64);
            self.persist_reference_block(&mut build, next, json!({"points":references}))
                .await?;
            next += 1;
        }
        let cadence = number(&input, "elapsed_seconds")?
            / (integer(&input, "minimum_passes_observed")?.max(2) - 1) as f64;
        self.publish_reference(p,payload,build,json!({"context_key":input["context_key"],"plan":plan(p)?,"points":points,"passes":input["minimum_passes_observed"],
            "boundary":input["boundary"],"pass_period_seconds":cadence,"level_unit":"s_meter_count","stronger":"lower",
            "threshold_sigma":3,"impulse_sigma":6,"confirmations":3,"window_passes":5,"detector_version":"ardv1-counts-v1"})).await
    }

    pub(crate) async fn validate_standalone_baseline(&self, p: &Value, b: &Value) -> Result<()> {
        let receiver = p["primary_source_kind"] == "ardv1";
        let expected = if receiver {
            let configured = plan(p)?;
            if !compatible_plan(&configured, &b["plan"]) {
                return Err(invalid("Изменился план приёма AR-DV1"));
            }
            (integer(&configured, "points")? as usize).div_ceil(BLOCK_POINTS)
        } else {
            let windows = integer(b, "windows")? as usize;
            let keys: Vec<i64> = serde_json::from_value(b["pass_keys"].clone())
                .map_err(|_| invalid("RF Explorer reference keys"))?;
            if !self
                .archive
                .rfe_guard_sources_valid(
                    s(b, "session_id")?,
                    s(b, "stream_id")?,
                    &keys,
                    s(b, "context_key")?,
                    windows,
                )
                .await?
            {
                return Err(invalid("Исходные проходы RF Explorer больше не совместимы"));
            }
            windows
        };
        if b["confirmed"] != true
            || b["project_id"] != p["id"]
            || b["primary_source_kind"] != p["primary_source_kind"]
            || expected == 0
            || b["block_count"] != expected
            || self
                .archive
                .duty_reference_block_count(s(p, "id")?, s(b, "id")?)
                .await?
                != expected
        {
            return Err(invalid("Эталон первичного прибора неполон"));
        }
        Ok(())
    }

    pub(crate) async fn analyze_receiver(&self, mut p: Value) -> Result<Value> {
        let id = s(&p, "id")?.to_owned();
        let sid = s(&p, "active_session_id")?.to_owned();
        let stream = s(&p, "receiver_stream_id")?.to_owned();
        let baseline = self
            .get_record("baseline", s(&p, "baseline_id")?, Some(&id))
            .await?;
        let issued = s(&baseline, "id")?;
        let state_prefix = format!("{issued}.{sid}");
        let pending = self
            .archive
            .ardv1_points_after(&sid, &stream, p["detector_rowid"].as_u64().unwrap_or(0), 64)
            .await?;
        let mut references = BTreeMap::new();
        let mut events = BTreeSet::new();
        let mut processed = 0;
        let mut incompatible = 0;
        let rules = self
            .archive
            .list_monitoring(
                "allowed",
                ori_archive::MonitoringQuery {
                    project_id: Some(id.clone()),
                    limit: 1000,
                    ..Default::default()
                },
            )
            .await?;
        for (sequence, measured) in pending {
            self.ensure_open()?;
            let obs = &measured["observation"];
            let metadata = &obs["metadata"];
            let pass = integer(metadata, "scan_pass")?;
            let index = integer(metadata, "scan_index")? as usize;
            let page = index / BLOCK_POINTS;
            let hz = integer(obs, "frequency_hz")?;
            let _tx = self.coordinator.lock().await;
            let mut current = self.project(&id).await?;
            if current["detector_rowid"] != p["detector_rowid"]
                || current["baseline_id"] != baseline["id"]
                || current["active_session_id"] != sid
            {
                return Err(GuardError::Conflict("Изменился курсор AR-DV1".into()));
            }
            if let Some(runtime) = self.runtime_snapshot(&id)?
                && (runtime.stopped || runtime.manual.contains_key(&stream))
            {
                break;
            }
            if measured["context_key"] != baseline["context_key"]
                || index >= integer(&baseline, "points")? as usize
            {
                incompatible += 1;
                if self.runtime_snapshot(&id)?.is_some() {
                    return Err(invalid("Контекст RX не совпал с эталоном AR-DV1"));
                }
            } else {
                if let std::collections::btree_map::Entry::Vacant(entry) = references.entry(page) {
                    entry.insert(
                        self.get_record(BLOCK, &block_id(issued, page), Some(&id))
                            .await?,
                    );
                }
                let point = &references[&page]["points"][index % BLOCK_POINTS];
                if point["frequency_hz"] != hz {
                    return Err(invalid("Точка не совпала с эталоном AR-DV1"));
                }
                let reference: CountReference = serde_json::from_value(point.clone())
                    .map_err(|_| invalid("Эталон отсчётов AR-DV1"))?;
                let checkpoint_id = block_id(&state_prefix, index);
                let mut state=self.archive.monitoring(DETECTOR,&checkpoint_id).await?.unwrap_or_else(||json!({
                    "id":checkpoint_id,"project_id":id,"created_at":self.clock.now().as_str(),"offset":0.0,"recent":[],"quiet":0}));
                let mut mutations = vec![];
                if state["last_pass"].as_u64().is_none_or(|last| pass > last) {
                    let quiet_rule = rules.iter().find(|r| {
                        r["start_hz"].as_u64().is_some_and(|n| n <= hz)
                            && r["end_hz"].as_u64().is_some_and(|n| hz <= n)
                    });
                    if let Some(event) = self
                        .receiver_point(
                            &p,
                            &baseline,
                            &reference,
                            &mut state,
                            obs,
                            quiet_rule,
                            &mut mutations,
                        )
                        .await?
                    {
                        events.insert(event);
                    }
                    mutations.push(Self::mutation(DETECTOR, state));
                    processed += 1;
                }
                if current["coverage"] == "restoring" {
                    current["coverage"] = json!("wide");
                }
                current["detector_rowid"] = json!(sequence);
                current["detector_cursor"] = obs["id"].clone();
                mutations.push(Self::mutation("project", current.clone()));
                self.archive.apply(mutations).await?;
                p = current;
                continue;
            }
            current["detector_rowid"] = json!(sequence);
            current["detector_cursor"] = obs["id"].clone();
            self.archive
                .put_monitoring("project", current.clone())
                .await?;
            p = current;
        }
        Ok(
            json!({"project_id":id,"processed_observations":processed,"excluded_observations":incompatible,"events_updated":events.len(),"event_ids":events,"cursor":p["detector_cursor"]}),
        )
    }

    #[allow(clippy::too_many_arguments)]
    async fn receiver_point(
        &self,
        p: &Value,
        b: &Value,
        reference: &CountReference,
        state: &mut Value,
        obs: &Value,
        rule: Option<&Value>,
        changes: &mut Vec<Mutation>,
    ) -> Result<Option<String>> {
        let id = s(p, "id")?;
        let pass = integer(&obs["metadata"], "scan_pass")?;
        let raw = raw_count(&obs["level"])?;
        let sample = reference.compare(raw, number(state, "offset")?);
        let source = json!({"session_id":obs["session_id"],"stream_id":obs["stream_id"],"observation_id":obs["id"],
            "frequency_hz":obs["frequency_hz"],"scan_pass":pass,"level":obs["level"],"excess_counts":sample.excess,"level_unit":"s_meter_count","interval":obs["interval"]});
        let interval = state["last_started_at"]
            .as_str()
            .map(|previous| elapsed(previous, s(&obs["interval"], "started_at")?))
            .transpose()?;
        let gap = state["last_pass"]
            .as_u64()
            .is_some_and(|last| last.checked_add(1) != Some(pass))
            || interval.is_some_and(|t| {
                t <= 0. || t > (3. * b["pass_period_seconds"].as_f64().unwrap_or(0.)).max(1.)
            })
            || state["generation"] != p["receiver_detector_generation"];
        state["generation"] = p["receiver_detector_generation"].clone();
        state["last_started_at"] = obs["interval"]["started_at"].clone();
        let previous = state["event_id"].as_str().map(str::to_owned);
        if gap {
            state["recent"] = json!([]);
            state["event_id"] = Value::Null;
            state["quiet"] = json!(0);
            if let Some(key) = previous.as_deref() {
                let mut event = self.event_record(id, key).await?;
                event["state"] = json!("interrupted");
                event["end_reason"] = json!("data_gap");
                changes.push(Self::mutation("event", event));
            }
        }
        state["last_pass"] = json!(pass);
        let recent = state["recent"]
            .as_array_mut()
            .ok_or_else(|| invalid("Контрольная точка AR-DV1"))?;
        recent.push(if sample.above {
            source.clone()
        } else {
            Value::Null
        });
        if recent.len() > 5 {
            recent.remove(0);
        }
        let evidence = recent
            .iter()
            .filter(|v| !v.is_null())
            .cloned()
            .collect::<Vec<_>>();
        let active = sample.above
            && (sample.impulse || evidence.len() >= 3)
            && !rule.is_some_and(|r| r["watch_changes"] == false);
        if !active {
            if !sample.above && state["event_id"].is_null() {
                state["offset"] = json!(sample.quiet_offset);
            }
            if let Some(key) = state["event_id"].as_str() {
                let key = key.to_owned();
                let quiet = if sample.above {
                    0
                } else {
                    state["quiet"].as_u64().unwrap_or(0) + 1
                };
                state["quiet"] = json!(quiet);
                if quiet >= 5 {
                    let mut event = self.event_record(id, &key).await?;
                    event["state"] = json!("closed");
                    changes.push(Self::mutation("event", event));
                    state["event_id"] = Value::Null;
                }
            }
            return Ok(None);
        }
        state["quiet"] = json!(0);
        let existing = state["event_id"].as_str();
        let mut event = if let Some(existing) = existing {
            self.event_record(id, existing).await?
        } else {
            let hz = &obs["frequency_hz"];
            self.record(Some(id),json!({"state":"open","primary_source_kind":"ardv1","disposition":"unreviewed","kind":if rule.is_some() {"authorized_changed"} else if sample.impulse {"impulse"}else{"new_signal"},
                "run_session_id":obs["session_id"],"scan_plan_id":obs["metadata"]["scan_plan_key"],"receiver_context_key":b["context_key"],"first_seen":evidence[0]["interval"]["started_at"],"baseline_id":b["id"],
                "start_frequency_hz":hz,"center_frequency_hz":hz,"end_frequency_hz":hz,"level_unit":"s_meter_count","source_count":0,"source_refs":[],"detector_version":"ardv1-counts-v1"}))?
        };
        let key = s(&event, "id")?.to_owned();
        let peak = if existing.is_some() {
            &source
        } else {
            evidence
                .iter()
                .min_by(|a, b| {
                    a["level"]
                        .as_f64()
                        .unwrap()
                        .total_cmp(&b["level"].as_f64().unwrap())
                })
                .unwrap_or(&source)
        };
        if event["peak_level"]
            .as_f64()
            .is_none_or(|old| peak["level"].as_f64().is_some_and(|v| v < old))
        {
            event["peak_level"] = json!(raw_count(&peak["level"])?);
            event["excess_counts"] = peak["excess_counts"].clone();
            event["sigma_counts"] = json!(reference.sigma());
            event["peak_source"] = peak.clone();
        }
        event["last_seen"] = obs["interval"]["ended_at"].clone();
        let evidence_count = evidence.len();
        let new_sources = if existing.is_some() {
            vec![source]
        } else {
            evidence
        };
        let count = integer(&event, "source_count")? + new_sources.len() as u64;
        let refs = event["source_refs"]
            .as_array_mut()
            .ok_or_else(|| invalid("Ссылки на наблюдения"))?;
        for item in new_sources {
            let mut record = item.clone();
            record["id"] = json!(format!("{key}.{}", s(&item, "observation_id")?));
            record["event_id"] = json!(key);
            record["created_at"] = self.clock.now().as_str().into();
            record["project_id"] = json!(id);
            changes.push(Self::mutation("event-source", record));
            refs.push(item);
        }
        if refs.len() > 100 {
            refs.drain(..refs.len() - 100);
        }
        event["source_count"] = json!(count);
        if matches!(p["state"].as_str(), Some("armed" | "degraded")) {
            changes.push(Mutation::EnqueueNotification(
                self.event_notification(p, &event)?,
            ));
            let occurrence = if evidence_count >= 3 {
                "sustained"
            } else if existing.is_some() {
                "repeat"
            } else {
                "impulse"
            };
            changes.push(
                self.duty_detection(p, &mut event, occurrence, s(obs, "id")?)
                    .await?,
            );
        }
        changes.push(Self::mutation("event", event));
        state["event_id"] = json!(key);
        Ok(Some(key))
    }
}

fn raw_count(v: &Value) -> Result<u32> {
    v.as_f64()
        .filter(|n| n.is_finite() && n.fract() == 0. && (0.0..=999999.).contains(n))
        .map(|v| v as u32)
        .ok_or_else(|| invalid("Неизвестный исходный отсчёт S-метра"))
}

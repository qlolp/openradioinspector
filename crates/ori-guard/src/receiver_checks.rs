use crate::{
    ControlError, GuardError, GuardService, Result, TuneReply, TuneRequest,
    control::IdleDispatch,
    qualification::{eligible_windows, source_qualification},
    receiver::ReceiverWork,
    runtime::ProjectRuntime,
    scan_plan::configured_plan,
    util::*,
};
use ori_archive::SessionLifecycleReceipt;
use ori_contracts::{Sweep, TimeInterval, UtcTimestamp};
use ori_monitoring::{Baseline, fingerprint};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::time::Duration;

const CONTROL_SWEEP_LIMIT: usize = 64;
const MAX_OFFSET_DB: f64 = 3.0;
pub(crate) const MAX_EVENT_AGE_SECONDS: f64 = 30.0;

/// Correlate archived receiver samples with a receipt without publishing the
/// worker's internal instance ID or the guard's control permit.
pub fn receiver_tune_context(instance: &str, revision: u64, observation_id: &str) -> String {
    let mut digest = Sha256::new();
    digest.update(revision.to_be_bytes());
    digest.update((instance.len() as u64).to_be_bytes());
    digest.update(instance.as_bytes());
    digest.update(observation_id.as_bytes());
    digest
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

#[derive(Clone)]
pub(crate) struct ControlBand {
    pub band_id: Option<String>,
    window_index: Option<u32>,
    baseline_id: String,
}

struct ReceiverCheck {
    band: ControlBand,
    baseline: Baseline,
    bin: Option<usize>,
    record: Value,
    lifecycle: SessionLifecycleReceipt,
}

struct ControlEvidence {
    receiver: Value,
    analyzer: Option<Value>,
}

fn rfe_band(project: &Value, source: &Value) -> Option<ControlBand> {
    let index = source
        .get("rfe_window_index")
        .or_else(|| source.get("scan_window_index"))?
        .as_u64()?;
    if index >= project["rfe_reference_windows"].as_u64()? {
        return None;
    }
    Some(ControlBand {
        band_id: None,
        window_index: Some(index as u32),
        baseline_id: crate::aor::block_id(project["baseline_id"].as_str()?, index as usize),
    })
}
fn accepted_bands(project: &Value) -> Result<Vec<ControlBand>> {
    if crate::rfe::survey(project) {
        return Ok(rfe_band(project, &project["detector_state"])
            .into_iter()
            .collect());
    }
    if let Some(plan) = configured_plan(project)? {
        let mut contexts = Vec::new();
        for band in &plan.bands {
            for index in eligible_windows(project, band) {
                if let Some(id) = project["band_baselines"][band.context_key(index)].as_str() {
                    contexts.push(ControlBand {
                        band_id: Some(band.id.clone()),
                        window_index: band.step_hz.map(|_| index),
                        baseline_id: id.to_owned(),
                    });
                }
            }
        }
        return Ok(contexts);
    }
    Ok(project["baseline_id"]
        .as_str()
        .map(|id| ControlBand {
            band_id: None,
            window_index: None,
            baseline_id: id.to_owned(),
        })
        .into_iter()
        .collect())
}

fn matches_band(source: &Value, project: &Value, band: &ControlBand) -> bool {
    if crate::rfe::survey(project) {
        let index = source["survey"]["tile"]
            .as_u64()
            .and_then(|n| n.checked_sub(1))
            .or_else(|| source["rfe_window_index"].as_u64())
            .or_else(|| source["window_index"].as_u64())
            .or_else(|| source["scan_window_index"].as_u64());
        return index.is_some() && index == band.window_index.map(u64::from);
    }
    if let Some(band_id) = &band.band_id {
        source["scan_band_id"] == *band_id
            && source["scan_window_index"] == json!(band.window_index)
            && source["scan_plan_id"] == project["scan_plan"]["id"]
            && source["scan_plan_revision"] == project["scan_plan"]["revision"]
    } else {
        source["scan_band_id"].is_null() && source["scan_plan_id"].is_null()
    }
}

fn scope_matches(project: &Value, runtime: &ProjectRuntime) -> bool {
    project["active_session_id"] == runtime.sid
        && project["analyzer_stream_id"] == runtime.analyzer
        && project["receiver_stream_id"] == runtime.receiver
}

fn compatible_sweep(
    sweep: &Sweep,
    project: &Value,
    band: &ControlBand,
    baseline: &Baseline,
    now: &UtcTimestamp,
) -> bool {
    let Some(settings) = &sweep.settings else {
        return false;
    };
    let Some(adapter) = settings.get("adapter").and_then(Value::as_str) else {
        return false;
    };
    if !source_qualification(project, sweep).is_ok_and(|q| q.eligible())
        || !matches!(
            adapter,
            "tinysa-serial" | "tinysa-sim" | "rf-explorer-3g-serial"
        )
        || project["profile"]["analyzer"]["settings"]["adapter"] != adapter
        || settings.get("simulated") != Some(&json!(adapter == "tinysa-sim"))
        || settings.get("replay") == Some(&Value::Bool(true))
        || settings
            .get("artifact_kind")
            .is_some_and(|kind| kind != "raw_spectrum")
        || sweep.interval.ended_at > *now
        || project["armed_at"]
            .as_str()
            .and_then(|value| timestamp(value).ok())
            .is_none_or(|armed| sweep.interval.started_at < armed)
    {
        return false;
    }
    let settings = json!(settings);
    matches_band(&settings, project, band)
        && sweep.frequencies_hz == baseline.frequencies_hz
        && fingerprint(sweep).is_ok_and(|actual| actual == baseline.fingerprint)
}

fn analyzer_evidence(sweep: &Sweep, bin: usize, baseline: &Baseline) -> Value {
    json!({
        "sweep_id": sweep.id,
        "session_id": sweep.session_id,
        "stream_id": sweep.stream_id,
        "interval": sweep.interval,
        "frequency_hz": sweep.frequencies_hz[bin],
        "level": sweep.levels[bin],
        "level_unit": sweep.level_unit,
        "baseline_p95_dbm": baseline.p95_dbm[bin],
    })
}

fn receiver_evidence(sample: &Value, simulated: bool) -> Value {
    let metadata = &sample["metadata"];
    json!({
        "observation_id": sample["id"],
        "session_id": sample["session_id"],
        "stream_id": sample["stream_id"],
        "interval": sample["interval"],
        "frequency_hz": sample["frequency_hz"],
        "mode": sample["mode"],
        "level": sample["level"],
        "level_unit": sample["level_unit"],
        "squelch_open": sample["squelch_open"],
        "raw_squelch_status": metadata["raw_squelch_status"],
        "decoded_mode": sample["decoded_mode"],
        "decoder_source": metadata["decoder_source"],
        "frequency_source": if simulated { json!("simulator") } else { metadata["frequency_source"].clone() },
        "frequency_readback": metadata["frequency_readback"] == true,
    })
}

pub(super) struct ReceiverMatch<'a> {
    receipt: &'a TuneReply,
    instance: &'a str,
    simulated: bool,
}

impl<'a> ReceiverMatch<'a> {
    pub(super) fn new(receipt: &'a TuneReply, project: &Value) -> Option<Self> {
        let instance = receipt.worker.worker_instance.as_deref()?;
        let adapter = project["profile"]["receiver"]["settings"]["adapter"].as_str()?;
        if receipt.acknowledged != Some(true)
            || receipt.applied_tune_revision == 0
            || !matches!(adapter, "ardv1-sim" | "ardv1-serial")
        {
            return None;
        }
        Some(Self {
            receipt,
            instance,
            simulated: adapter == "ardv1-sim",
        })
    }

    pub(super) fn interval(&self, sample: &Value, end: &UtcTimestamp) -> Option<TimeInterval> {
        let metadata = &sample["metadata"];
        let level = sample["level"].as_f64()?;
        if sample["session_id"] != self.receipt.worker.session_id
            || sample["stream_id"] != self.receipt.worker.stream_id
            || sample["frequency_hz"].as_u64() != self.receipt.tuned.get("frequency_hz")?.as_u64()
            || sample["mode"].as_str() != self.receipt.tuned.get("mode")?.as_str()
            || sample["level_unit"] != "s_meter_count"
            || !(0.0..=f64::from(u32::MAX)).contains(&level)
            || level.fract() != 0.0
            || !sample["squelch_open"].is_boolean()
            || metadata["simulated"] != self.simulated
            || metadata["replay"] == true
        {
            return None;
        }
        let adapter = if self.simulated {
            "ardv1-sim"
        } else {
            "ardv1-serial"
        };
        if metadata["adapter"] != adapter {
            return None;
        }
        let observation_id = sample["id"].as_str()?;
        if metadata["receiver_tune_context"]
            != receiver_tune_context(
                self.instance,
                self.receipt.applied_tune_revision,
                observation_id,
            )
        {
            return None;
        }
        if !self.simulated {
            let squelch = metadata["raw_squelch_status"].as_u64()?;
            if metadata["frequency_readback"] != true
                || metadata["frequency_source"] != "receiver_RX"
                || metadata["mode_source"] != "receiver_RX"
                || metadata["raw_receiver_status"]
                    .as_str()
                    .is_none_or(str::is_empty)
                || squelch > 3
                || sample["squelch_open"] != (squelch != 0)
            {
                return None;
            }
        }
        let interval: TimeInterval = decode(&sample["interval"]).ok()?;
        (interval.started_at >= self.receipt.acknowledged_at
            && interval.started_at <= interval.ended_at
            && interval.ended_at <= *end)
            .then_some(interval)
    }

    fn activity(&self, sample: &Value) -> ReceiverActivity {
        if self.simulated {
            return if sample["squelch_open"] == false {
                ReceiverActivity::Absent
            } else if sample["level"].as_f64().is_some_and(|level| level > 0.0) {
                ReceiverActivity::Present
            } else {
                ReceiverActivity::Ambiguous
            };
        }
        let metadata = &sample["metadata"];
        let decoded = metadata["decoder_source"] == "receiver_RX"
            && sample["decoded_mode"]
                .as_str()
                .is_some_and(|mode| !mode.is_empty())
            && metadata["raw_mode_status"]
                .as_str()
                .and_then(|status| status.as_bytes().get(2))
                .is_some_and(|digit| matches!(digit, b'1'..=b'7'));
        match metadata["raw_squelch_status"].as_u64() {
            Some(0) if !decoded => ReceiverActivity::Absent,
            Some(2) => ReceiverActivity::Present,
            Some(1 | 3) if decoded => ReceiverActivity::Present,
            // Noise or level squelch can be set fully open. A raw count and
            // that setting alone do not demonstrate receiver signal activity.
            _ => ReceiverActivity::Ambiguous,
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum ReceiverActivity {
    Absent,
    Ambiguous,
    Present,
}

pub(crate) fn verification(
    analysis: &mut Value,
    state: &str,
    reason: &str,
    analyzer: Value,
    receiver: Value,
    simulated: Option<bool>,
) {
    analysis["verification"] = json!(state);
    analysis["verification_evidence"] = json!({
        "reason": reason,
        "analyzer": analyzer,
        "receiver": receiver,
        "simulated": simulated,
        "hardware_validation": "not_run",
    });
}

impl GuardService {
    async fn accepted_baseline(
        &self,
        project: &Value,
        band: &ControlBand,
    ) -> Result<Option<Baseline>> {
        let Some(raw) = self
            .archive
            .monitoring(
                if crate::rfe::survey(project) {
                    crate::aor::BLOCK
                } else {
                    "baseline"
                },
                &band.baseline_id,
            )
            .await?
        else {
            return Ok(None);
        };
        if raw["confirmed"] != true
            || raw["project_id"] != project["id"]
            || !matches_band(&raw, project, band)
        {
            return Ok(None);
        }
        let baseline: Baseline = decode(&raw)?;
        let bins = baseline.frequencies_hz.len();
        if baseline.p95_dbm.len() != bins || baseline.mad_db.len() != bins {
            return Ok(None);
        }
        Ok(Some(baseline))
    }

    pub(crate) async fn receiver_session_running(&self, runtime: &ProjectRuntime) -> Result<bool> {
        Ok(self
            .archive
            .get_session(&runtime.sid)
            .await?
            .is_some_and(|session| {
                session["state"] == "running"
                    && session["metadata"]["monitoring_project_id"] == runtime.cached_project["id"]
                    && session["metadata"]["replay"] != true
                    && session["metadata"]["source_search"] != true
            }))
    }

    pub(crate) async fn verify_event_receipt(
        &self,
        project: &Value,
        runtime: &ProjectRuntime,
        event: &Value,
        receipt: &TuneReply,
        observations: &[Value],
        analysis: &mut Value,
    ) -> Result<()> {
        let mut reason = "missing_receiver_readback";
        let matcher = ReceiverMatch::new(receipt, project);
        let simulated = matcher.as_ref().map(|matcher| matcher.simulated);
        let now = self.clock.now();
        let dwell_end = timestamp(s(analysis, "ended_at")?)?.min(now.clone());
        let mut best = None;
        if let Some(matcher) = matcher
            && scope_matches(project, runtime)
            && event["run_session_id"] == runtime.sid
            && event["center_frequency_hz"] == analysis["detected_frequency_hz"]
        {
            let samples = observations
                .iter()
                .rev()
                .filter_map(|sample| {
                    matcher
                        .interval(sample, &dwell_end)
                        .map(|interval| (sample, interval))
                })
                .collect::<Vec<_>>();
            if !samples.is_empty() {
                reason = "no_coincident_tinysa_source";
                best = self
                    .coincident_event_source(project, runtime, event, &matcher, &samples, &now)
                    .await?;
            }
        }
        let (state, reason, analyzer, receiver) = match best {
            Some((activity, analyzer, receiver)) => {
                let (state, reason) = match activity {
                    ReceiverActivity::Present => {
                        ("observed_both", "coincident_receipt_bound_observations")
                    }
                    ReceiverActivity::Absent => (
                        "not_observed",
                        "receiver_squelch_closed_during_tinysa_detection",
                    ),
                    ReceiverActivity::Ambiguous => ("inconclusive", "receiver_activity_ambiguous"),
                };
                (state, reason, analyzer, receiver)
            }
            None => ("inconclusive", reason, Value::Null, Value::Null),
        };
        verification(analysis, state, reason, analyzer, receiver, simulated);
        Ok(())
    }

    async fn coincident_event_source(
        &self,
        project: &Value,
        runtime: &ProjectRuntime,
        event: &Value,
        matcher: &ReceiverMatch<'_>,
        samples: &[(&Value, TimeInterval)],
        now: &UtcTimestamp,
    ) -> Result<Option<(ReceiverActivity, Value, Value)>> {
        let band = if crate::rfe::survey(project) {
            rfe_band(project, event).filter(|b| event["baseline_id"] == b.baseline_id)
        } else {
            accepted_bands(project)?.into_iter().find(|band| {
                event["baseline_id"] == band.baseline_id && matches_band(event, project, band)
            })
        };
        let Some(band) = band else {
            return Ok(None);
        };
        let Some(baseline) = self.accepted_baseline(project, &band).await? else {
            return Ok(None);
        };
        let frequency = integer(event, "center_frequency_hz")?;
        let Ok(bin) = baseline.frequencies_hz.binary_search(&frequency) else {
            return Ok(None);
        };
        let margin = number(&event["detector_parameters"], "margin_db")?;
        let threshold =
            baseline.p95_dbm[bin] + MAX_OFFSET_DB + margin.max(3.0 * 1.4826 * baseline.mad_db[bin]);
        let refs = event["source_refs"]
            .as_array()
            .ok_or_else(|| invalid("event source_refs"))?;
        let mut best = None;
        for reference in refs.iter().rev().take(100) {
            if reference["session_id"] != runtime.sid || reference["stream_id"] != runtime.analyzer
            {
                continue;
            }
            let Some(raw) = self.archive.get_sweep(s(reference, "sweep_id")?).await? else {
                continue;
            };
            let sweep: Sweep = decode(&raw)?;
            if sweep.session_id != runtime.sid || sweep.stream_id != runtime.analyzer {
                continue;
            }
            let matching = samples.iter().filter(|(_, sample)| {
                sample.started_at <= sweep.interval.ended_at
                    && sweep.interval.started_at <= sample.ended_at
            });
            let Some((activity, sample)) = matching
                .map(|(sample, _)| (matcher.activity(sample), *sample))
                .max_by_key(|(activity, _)| *activity)
            else {
                continue;
            };
            if best
                .as_ref()
                .is_some_and(|(previous, _, _)| *previous >= activity)
                || sweep.levels.get(bin).is_none_or(|level| *level < threshold)
                || !compatible_sweep(&sweep, project, &band, &baseline, now)
            {
                continue;
            }
            best = Some((
                activity,
                analyzer_evidence(&sweep, bin, &baseline),
                receiver_evidence(sample, matcher.simulated),
            ));
            if activity == ReceiverActivity::Present {
                break;
            }
        }
        Ok(best)
    }

    pub(crate) fn check_idle_receiver(
        &self,
        id: &str,
        runtime: &ProjectRuntime,
    ) -> Result<ProjectRuntime> {
        let current = self.check_job(id, &runtime.token, runtime.epoch, &runtime.receiver)?;
        if current.sid != runtime.sid
            || current.analyzer_epoch != runtime.analyzer_epoch
            || !idle_receiver_available(&current)
        {
            return Err(ControlError::Denied.into());
        }
        Ok(current)
    }

    // Called from the receiver scheduler under the coordinator after the
    // pending-event query. Reserve exactly one point per job and recheck the
    // event queue before any later point.
    pub(crate) async fn queue_receiver_check(
        &self,
        id: &str,
        project: &Value,
        runtime: &ProjectRuntime,
    ) -> Result<()> {
        if !idle_receiver_available(runtime)
            || project["coverage"] != "wide"
            || !scope_matches(project, runtime)
        {
            return Ok(());
        }
        for band in accepted_bands(project)? {
            if runtime.receiver_checks_done.contains(&band.band_id) {
                continue;
            }
            let detector = match &band.band_id {
                Some(band) => &project["band_detectors"][band],
                None => &project["detector_state"],
            };
            if band.window_index.is_some()
                && detector["scan_window_index"] != json!(band.window_index)
            {
                continue;
            }
            let Some(source) = detector["recent_sources"]
                .as_array()
                .and_then(|items| items.last())
            else {
                continue;
            };
            if source["session_id"] != runtime.sid || source["stream_id"] != runtime.analyzer {
                continue;
            }
            let Some(baseline) = self.accepted_baseline(project, &band).await? else {
                continue;
            };
            let Some(raw) = self.archive.get_sweep(s(source, "sweep_id")?).await? else {
                continue;
            };
            let sweep: Sweep = decode(&raw)?;
            if sweep.session_id != runtime.sid
                || sweep.stream_id != runtime.analyzer
                || runtime
                    .receiver_checks_resume_after
                    .as_ref()
                    .is_some_and(|at| sweep.interval.started_at <= *at)
                || !compatible_sweep(&sweep, project, &band, &baseline, &self.clock.now())
                || !fresh_control_source(
                    &sweep.interval.ended_at,
                    project,
                    &baseline,
                    &self.clock.now(),
                )
            {
                continue;
            }
            let bin = control_bin(&baseline.frequencies_hz);
            if bin.is_some_and(|bin| sweep.levels[bin] > baseline.p95_dbm[bin] + MAX_OFFSET_DB) {
                continue;
            }
            let mut record = self.record(Some(id), json!({
                "session_id": runtime.sid,
                "baseline_id": band.baseline_id,
                "scan_band_id": band.band_id,
                "scan_plan_id": project["scan_plan"]["id"],
                "scan_plan_revision": project["scan_plan"]["revision"],
                "requested_mode": project.get("receiver_mode").and_then(Value::as_str).unwrap_or("AUTO"),
                "frequency_hz": null,
                "analyzer": null,
                "analyzer_anchor": null,
                "receiver": null,
                "simulated": project["profile"]["receiver"]["settings"]["adapter"] == "ardv1-sim",
                "hardware_validation": "not_run",
            }))?;
            if let Some(index) = band.window_index {
                record["scan_window_index"] = json!(index);
            }
            if let Some(bin) = bin {
                record["frequency_hz"] = json!(crate::receiver::receiver_frequency(
                    sweep.frequencies_hz[bin]
                )?);
                record["analyzer_anchor"] = analyzer_evidence(&sweep, bin, &baseline);
            }
            let lifecycle = self.archive.capture_session_lifecycle(&runtime.sid).await?;
            let owned = self.weak.upgrade().ok_or(ControlError::Denied)?;
            let project_id = id.to_owned();
            let snapshot = runtime.clone();
            let check = ReceiverCheck {
                band: band.clone(),
                baseline,
                bin,
                record,
                lifecycle,
            };
            return self.spawn_receiver_job(id, runtime, ReceiverWork::Check(band), async move {
                owned
                    .run_receiver_check(&project_id, &snapshot, check)
                    .await
            });
        }
        Ok(())
    }

    async fn run_receiver_check(
        &self,
        id: &str,
        runtime: &ProjectRuntime,
        mut check: ReceiverCheck,
    ) -> Result<()> {
        let mode = s(&check.record, "requested_mode")?.to_owned();
        let frequency = check.record["frequency_hz"].as_u64();
        let mut receipt = None;
        let result: Result<Option<ControlEvidence>> = async {
            self.check_idle_receiver(id, runtime)?;
            let Some(frequency_hz) = frequency else {
                return Ok(None);
            };
            let boundary = self.archive.sweep_ingestion_boundary(&runtime.sid).await?;
            self.check_idle_receiver(id, runtime)?;
            if !check.lifecycle.check() {
                return Err(ControlError::Denied.into());
            }
            let target = Self::target(runtime, &runtime.receiver);
            let mut permit = self.permit(id, runtime);
            permit.idle_dispatch = Some(IdleDispatch {
                analyzer_stream_id: runtime.analyzer.clone(),
                analyzer_epoch: runtime.analyzer_epoch,
                lifecycle: check.lifecycle.clone(),
            });
            let reply = self
                .control
                .tune(TuneRequest {
                    target,
                    permit,
                    frequency_hz,
                    mode: mode.clone(),
                })
                .await?;
            if reply.acknowledged != Some(true)
                || reply.worker.session_id != runtime.sid
                || reply.worker.stream_id != runtime.receiver
                || reply.tuned.get("frequency_hz").and_then(Value::as_u64) != Some(frequency_hz)
                || reply.tuned.get("mode").and_then(Value::as_str) != Some(mode.as_str())
            {
                return Err(ControlError::Unavailable("acknowledged_tune").into());
            }
            self.control.validate_tune(&reply)?;
            receipt = Some(reply);
            // The overlapping sweep must be committed before evidence can be
            // read. Allow for an acquisition interval and archive latency.
            let dwell = Duration::from_secs_f64(
                (2.0 * baseline_sweep_period(&check.baseline)).clamp(2.0, MAX_EVENT_AGE_SECONDS),
            );
            let deadline = self.clock.monotonic() + dwell;
            let wall_deadline = tokio::time::Instant::now() + dwell;
            while self.clock.monotonic() < deadline && tokio::time::Instant::now() < wall_deadline {
                self.check_idle_receiver(id, runtime)?;
                if !check.lifecycle.check() {
                    return Err(ControlError::Denied.into());
                }
                tokio::select! {
                    _ = tokio::time::sleep(Duration::from_millis(50)) => {},
                    _ = self.wake.notified() => {},
                }
            }
            self.check_idle_receiver(id, runtime)?;
            if !check.lifecycle.check() {
                return Err(ControlError::Denied.into());
            }
            let end = self.clock.now();
            self.collect_control_evidence(
                runtime,
                &check,
                receipt.as_ref().ok_or(ControlError::Denied)?,
                boundary,
                &end,
            )
            .await
        }
        .await;
        let _tx = self.coordinator.lock().await;
        let project = self.project(id).await?;
        let current_scope = scope_matches(&project, runtime)
            && project["coverage"] == "wide"
            && matches_band(&check.record, &project, &check.band)
            && (if crate::rfe::survey(&project) {
                rfe_band(&project, &check.record).into_iter().collect()
            } else {
                accepted_bands(&project)?
            })
            .into_iter()
            .any(|band| {
                band.band_id == check.band.band_id && band.baseline_id == check.band.baseline_id
            })
            && self.check_idle_receiver(id, runtime).is_ok()
            && check.lifecycle.check();
        let current_receipt = receipt
            .as_ref()
            .is_none_or(|receipt| self.control.validate_tune(receipt).is_ok());
        let (state, reason) = if !current_scope || !current_receipt {
            ("interrupted", "control_context_revoked")
        } else {
            match result {
                Ok(Some(evidence)) => {
                    check.record["receiver"] = evidence.receiver;
                    if let Some(analyzer) = evidence.analyzer {
                        check.record["analyzer"] = analyzer;
                        (
                            "sampled",
                            "coincident_quiet_analyzer_and_receiver_observations",
                        )
                    } else {
                        ("inconclusive", "no_coincident_tinysa_source")
                    }
                }
                Ok(None) if frequency.is_none() => {
                    ("unsupported_frequency", "no_supported_control_frequency")
                }
                Ok(None) => ("no_observations", "missing_receiver_readback"),
                Err(GuardError::Control(_)) => ("unavailable", "receiver_tune_unavailable"),
                Err(error) => return Err(error),
            }
        };
        check.record["state"] = json!(state);
        check.record["reason"] = json!(reason);
        check.record["ended_at"] = json!(self.clock.now());
        self.archive
            .put_monitoring("receiver-check", check.record)
            .await?;
        self.finish_receiver_job(id, &runtime.token, state != "interrupted")
    }

    async fn collect_control_evidence(
        &self,
        runtime: &ProjectRuntime,
        check: &ReceiverCheck,
        receipt: &TuneReply,
        boundary: u64,
        end: &UtcTimestamp,
    ) -> Result<Option<ControlEvidence>> {
        let bin = check.bin.ok_or_else(|| invalid("receiver check bin"))?;
        let project = &runtime.cached_project;
        let Some(matcher) = ReceiverMatch::new(receipt, project) else {
            return Ok(None);
        };
        let observations = self
            .archive
            .list_observations(&runtime.sid, Some(&runtime.receiver), 1000)
            .await?;
        let samples = observations
            .iter()
            .rev()
            .filter_map(|sample| {
                matcher
                    .interval(sample, end)
                    .map(|interval| (sample, interval))
            })
            .collect::<Vec<_>>();
        let Some((latest, _)) = samples.first() else {
            return Ok(None);
        };
        let boundary_sweeps = self
            .archive
            .sweeps_after_ingestion(
                &runtime.sid,
                Some(&runtime.analyzer),
                boundary,
                CONTROL_SWEEP_LIMIT,
            )
            .await;
        let sweeps = match boundary_sweeps {
            Ok(sweeps) => sweeps,
            // An optional check can fail to form a pair within its bounded
            // read without losing the receiver observation already captured.
            Err(ori_archive::ArchiveError::QueryBudget) => Vec::new(),
            Err(error) => return Err(error.into()),
        };
        for (_, raw) in sweeps.into_iter().rev() {
            let sweep: Sweep = decode(&raw)?;
            if sweep.session_id != runtime.sid
                || sweep.stream_id != runtime.analyzer
                || sweep
                    .levels
                    .get(bin)
                    .is_none_or(|level| *level > check.baseline.p95_dbm[bin] + MAX_OFFSET_DB)
                || !compatible_sweep(&sweep, project, &check.band, &check.baseline, end)
            {
                continue;
            }
            if let Some((sample, _)) = samples.iter().find(|(_, sample)| {
                sample.started_at <= sweep.interval.ended_at
                    && sweep.interval.started_at <= sample.ended_at
            }) {
                return Ok(Some(ControlEvidence {
                    receiver: receiver_evidence(sample, matcher.simulated),
                    analyzer: Some(analyzer_evidence(&sweep, bin, &check.baseline)),
                }));
            }
        }
        Ok(Some(ControlEvidence {
            receiver: receiver_evidence(latest, matcher.simulated),
            analyzer: None,
        }))
    }

    pub(crate) async fn receiver_checks_status(
        &self,
        id: &str,
        project: &Value,
        runtime: Option<&ProjectRuntime>,
    ) -> Result<Value> {
        if !project["receiver_stream_id"].is_string() || !project["analyzer_stream_id"].is_string()
        {
            return Ok(
                json!({"state":"unavailable","active_band_id":null,"completed":0,"total":0,"items":[]}),
            );
        }
        let bands = accepted_bands(project)?;
        // A logical band gets one idle receiver check per run, even if the
        // analyzer revisits it in hundreds of separately referenced windows.
        let total = bands
            .iter()
            .map(|band| &band.band_id)
            .collect::<std::collections::BTreeSet<_>>()
            .len();
        let mut items = self
            .archive
            .latest_receiver_checks(id, s(project, "active_session_id")?)
            .await?;
        items.retain(|record| {
            if crate::rfe::survey(project) {
                return rfe_band(project, record)
                    .is_some_and(|b| record["baseline_id"] == b.baseline_id);
            }
            bands.iter().any(|band| {
                record["baseline_id"] == band.baseline_id && matches_band(record, project, band)
            })
        });
        let completed = items
            .iter()
            .filter(|record| {
                matches!(
                    record["state"].as_str(),
                    Some(
                        "sampled"
                            | "inconclusive"
                            | "unsupported_frequency"
                            | "no_observations"
                            | "unavailable"
                    )
                )
            })
            .count();
        let active = match runtime.and_then(|r| r.receiver_work.as_ref()) {
            Some(ReceiverWork::Check(band)) => Some(band),
            _ => None,
        };
        let state = match runtime {
            None => "inactive",
            Some(r) if r.stopped => "inactive",
            Some(r) if !idle_receiver_available(r) || project["coverage"] != "wide" => "paused",
            Some(_) if active.is_some() => "checking",
            Some(_) if completed == total => "complete",
            Some(_) => "waiting",
        };
        Ok(json!({
            "state": state,
            "active_band_id": active.and_then(|band| band.band_id.as_deref()),
            "completed": completed,
            "total": total,
            "items": items,
        }))
    }
}

fn idle_receiver_available(runtime: &ProjectRuntime) -> bool {
    runtime.search.is_none()
        && runtime.manual.is_empty()
        && !runtime.faults.contains_key(&runtime.receiver)
        && !runtime.faults.contains_key(&runtime.analyzer)
}

fn control_bin(frequencies: &[u64]) -> Option<usize> {
    let low = (*frequencies.first()?).max(100_000);
    let high = (*frequencies.last()?).min(1_300_000_000);
    let midpoint = low + high.checked_sub(low)? / 2;
    frequencies
        .iter()
        .enumerate()
        .filter(|(_, hz)| {
            (low..=high).contains(hz)
                && crate::receiver::receiver_frequency(**hz)
                    .is_ok_and(|rounded| (low..=high).contains(&rounded))
        })
        .min_by_key(|(_, hz)| hz.abs_diff(midpoint))
        .map(|(bin, _)| bin)
}

fn baseline_sweep_period(baseline: &Baseline) -> f64 {
    baseline
        .extra
        .get("sweep_period_seconds")
        .and_then(Value::as_f64)
        .filter(|period| period.is_finite() && *period > 0.0)
        .unwrap_or(0.0)
}

fn fresh_control_source(
    end: &UtcTimestamp,
    project: &Value,
    baseline: &Baseline,
    now: &UtcTimestamp,
) -> bool {
    let period = baseline_sweep_period(baseline);
    let bands = project["scan_plan"]["config"]["bands"]
        .as_array()
        .map_or(1, Vec::len);
    let maximum = (period * 3.0).clamp(5.0, if bands > 1 { 120.0 } else { 30.0 });
    elapsed(end.as_str(), now.as_str()).is_ok_and(|age| (0.0..=maximum).contains(&age))
}

#[cfg(test)]
mod tests {
    use super::control_bin;

    #[test]
    fn control_tune_quantization_stays_inside_the_measurement_band() {
        assert_eq!(control_bin(&[100_003, 100_005, 100_006]), None);
        assert_eq!(control_bin(&[100_003, 100_005, 100_010]), Some(1));
        assert_eq!(
            control_bin(&[1_299_980_000, 1_300_000_000, 1_300_010_000]),
            Some(0)
        );
    }
}

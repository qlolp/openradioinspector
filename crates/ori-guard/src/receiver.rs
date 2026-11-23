use crate::{
    control::EventDispatch,
    receiver_checks::{ControlBand, MAX_EVENT_AGE_SECONDS, ReceiverMatch, verification},
    runtime::{JobKind, ProjectRuntime},
    util::*,
    *,
};
use ori_archive::{Artifact, Mutation};
use ori_session::{SessionEffects, SessionError, SessionFuture, TuneCompletionCheck, TuneReceipt};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    sync::{Arc, Mutex, Weak},
    time::Duration,
};

#[derive(Clone)]
pub(crate) enum ReceiverWork {
    Event(Arc<EventDispatch>),
    Check(ControlBand),
}

pub(crate) struct GuardEffects {
    pub guard: Weak<GuardService>,
    pub control: Arc<dyn InstrumentControl>,
    pub context: Option<Arc<TuneContext>>,
}
pub(crate) struct TuneContext {
    target: ControlTarget,
    permit: ControlPermit,
    dispatch: Arc<EventDispatch>,
    state: Mutex<TuneState>,
}
#[derive(Default)]
struct TuneState {
    check: Option<Arc<dyn TuneCompletionCheck>>,
    original_reply: Option<TuneReply>,
}
struct Completion {
    guard: Weak<GuardService>,
    control: Arc<dyn InstrumentControl>,
    target: ControlTarget,
    permit: ControlPermit,
    reply: TuneReply,
}
fn session_error(error: ControlError) -> SessionError {
    let status = if matches!(error, ControlError::Denied) {
        409
    } else {
        503
    };
    SessionError::new(status, "guard_control_unavailable", error.to_string())
}
fn domain_error(error: SessionError) -> GuardError {
    GuardError::Conflict(error.message)
}
impl TuneCompletionCheck for Completion {
    fn revalidate(&self) -> ori_session::SessionResult<()> {
        let guard = self
            .guard
            .upgrade()
            .ok_or_else(|| session_error(ControlError::Denied))?;
        guard
            .authorize_control(&self.target, Some(&self.permit))
            .map_err(session_error)?;
        self.control
            .validate_tune(&self.reply)
            .map_err(session_error)
    }
}
impl SessionEffects for GuardEffects {
    fn tune(&self, request: ori_session::TuneRequest) -> SessionFuture<'_, TuneReceipt> {
        Box::pin(async move {
            let context = self.context.as_ref().ok_or_else(|| {
                SessionError::new(
                    503,
                    "guard_original_permit_required",
                    "Для исследования нужен исходный permit задания",
                )
            })?;
            if request.session_id != context.target.session_id
                || request.stream_id != context.target.stream_id
            {
                return Err(session_error(ControlError::Denied));
            }
            let guard = self
                .guard
                .upgrade()
                .ok_or_else(|| session_error(ControlError::Denied))?;
            let mut dispatch_permit = context.permit.clone();
            dispatch_permit.event_dispatch = Some(context.dispatch.clone());
            guard
                .authorize_control(&context.target, Some(&dispatch_permit))
                .map_err(session_error)?;
            let frequency_hz = request.frequency_hz;
            let mode = request.mode.unwrap_or_else(|| "AUTO".into());
            let reply = self
                .control
                .tune(TuneRequest {
                    target: context.target.clone(),
                    permit: dispatch_permit,
                    frequency_hz,
                    mode: mode.clone(),
                })
                .await
                .map_err(session_error)?;
            if reply.acknowledged != Some(true)
                || reply.worker.session_id != context.target.session_id
                || reply.worker.stream_id != context.target.stream_id
                || reply.tuned.get("frequency_hz").and_then(Value::as_u64) != Some(frequency_hz)
                || reply.tuned.get("mode").and_then(Value::as_str) != Some(mode.as_str())
            {
                return Err(session_error(ControlError::Unavailable(
                    "acknowledged_tune",
                )));
            }
            let check: Arc<dyn TuneCompletionCheck> = Arc::new(Completion {
                guard: self.guard.clone(),
                control: self.control.clone(),
                target: context.target.clone(),
                permit: context.permit.clone(),
                reply: reply.clone(),
            });
            let mut state = context
                .state
                .lock()
                .map_err(|_| session_error(ControlError::Denied))?;
            state.check = Some(check.clone());
            state.original_reply = Some(reply);
            Ok(TuneReceipt::new(check))
        })
    }
    fn validate_reference(&self, _session: Value, _sweep: Value) -> SessionFuture<'_, ()> {
        Box::pin(async {
            Err(SessionError::new(
                503,
                "guard_reference_unavailable",
                "Проверка научного эталона не входит в закрытый фасад охраны",
            ))
        })
    }
}

pub(crate) fn receiver_frequency(frequency: u64) -> Result<u64> {
    if !(100_000..=1_300_000_000).contains(&frequency) {
        return Err(invalid("Событие вне диапазона AR-DV1"));
    }
    Ok(((frequency + 5) / 10) * 10)
}

impl GuardService {
    pub fn receiver_target(event: &Value) -> Result<u64> {
        receiver_frequency(integer(event, "center_frequency_hz")?)
    }
    pub(crate) fn active_event_dispatch(&self, id: &str) -> Result<Option<Arc<EventDispatch>>> {
        Ok(self
            .runtime_snapshot(id)?
            .and_then(|runtime| match runtime.receiver_work {
                Some(ReceiverWork::Event(dispatch)) => Some(dispatch),
                _ => None,
            }))
    }
    pub(crate) async fn queue_receiver(&self, id: &str) -> Result<()> {
        let _tx = self.coordinator.lock().await;
        self.ensure_open()?;
        let Some(r) = self.runtime_snapshot(id)?.filter(|r| !r.stopped) else {
            return Ok(());
        };
        if !r.paired()
            || r.receiver_work.is_some()
            || r.manual.contains_key(&r.receiver)
            || r.faults.contains_key(&r.receiver)
            || !self.receiver_job_idle(id)?
            || !self.receiver_session_running(&r).await?
        {
            return Ok(());
        }
        let p = self.project(id).await?;
        let Some(mut event) = self
            .archive
            .pending_receiver_events(id, &r.sid)
            .await?
            .into_iter()
            .next()
        else {
            return self.queue_receiver_check(id, &p, &r).await;
        };
        let checked_at = self.clock.monotonic();
        let age = elapsed(s(&event, "last_seen")?, self.clock.now().as_str())?;
        let mode = p
            .get("receiver_mode")
            .and_then(Value::as_str)
            .unwrap_or("AUTO");
        let target = Self::receiver_target(&event);
        let (state, reason, outcome, code) = if let Err(error) = &target {
            (
                "unsupported_frequency",
                Some(error.to_string()),
                "unsupported_frequency",
                "outside_receiver_range",
            )
        } else if !crate::qualification::event_eligible(&p, &event)?
            || !self
                .event_reference_eligible(&p, &event, &mut Default::default())
                .await?
        {
            (
                "inconclusive",
                Some(crate::qualification::unverified_reference().to_string()),
                "inconclusive",
                "reference_required_above_800_mhz",
            )
        } else if age > MAX_EVENT_AGE_SECONDS {
            (
                "expired",
                Some("Превышено допустимое время до перестройки".into()),
                "inconclusive",
                "event_expired",
            )
        } else if age < 0.0 {
            (
                "failed",
                Some("Время события позже момента проверки".into()),
                "inconclusive",
                "event_time_in_future",
            )
        } else {
            ("queued", None, "inconclusive", "verification_pending")
        };
        event["analysis"] = json!({
            "state": state, "requested_mode": mode, "event_age_seconds": age,
        });
        if let Some(reason) = reason {
            event["analysis"]["reason"] = json!(reason);
        }
        verification(
            &mut event["analysis"],
            outcome,
            code,
            Value::Null,
            Value::Null,
            None,
        );
        if state != "queued" {
            self.archive.put_monitoring("event", event).await?;
            return Ok(());
        }
        self.check_job(id, &r.token, r.epoch, &r.receiver)?;
        self.archive.put_monitoring("event", event.clone()).await?;
        let owned = self.weak.upgrade().ok_or(ControlError::Denied)?;
        let project_id = id.to_owned();
        let snapshot = r.clone();
        let dispatch = Arc::new(EventDispatch {
            event_id: s(&event, "id")?.to_owned(),
            center_hz: integer(&event, "center_frequency_hz")?,
            deadline: checked_at
                .saturating_add(Duration::from_secs_f64(MAX_EVENT_AGE_SECONDS - age)),
            reviewed: Default::default(),
            retargeted: Default::default(),
        });
        let event_id = dispatch.event_id.clone();
        let spawned =
            self.spawn_receiver_job(id, &r, ReceiverWork::Event(dispatch.clone()), async move {
                owned
                    .investigate(&project_id, event, snapshot, dispatch)
                    .await
            });
        if spawned.is_err() {
            let mut event = self.event_record(id, &event_id).await?;
            event["analysis"]["state"] = json!("interrupted");
            verification(
                &mut event["analysis"],
                "inconclusive",
                "control_context_revoked",
                Value::Null,
                Value::Null,
                None,
            );
            self.archive.put_monitoring("event", event).await?;
        }
        spawned
    }

    pub(crate) fn spawn_receiver_job(
        &self,
        id: &str,
        r: &ProjectRuntime,
        work: ReceiverWork,
        job: impl std::future::Future<Output = Result<()>> + Send + 'static,
    ) -> Result<()> {
        let owned = self.weak.upgrade().ok_or(ControlError::Denied)?;
        self.update_runtime(id, |runtime| {
            if runtime.token != r.token
                || runtime.epoch != r.epoch
                || runtime.stopped
                || runtime.receiver_work.is_some()
                || runtime.manual.contains_key(&runtime.receiver)
            {
                return Err(ControlError::Denied.into());
            }
            runtime.receiver_work = Some(work);
            Ok(())
        })?;
        let project_id = id.to_owned();
        let token = r.token.clone();
        let spawned = self.spawn_job(id, JobKind::Receiver, async move {
            let result = job.await;
            if let Err(error) = &result
                && owned
                    .runtime_snapshot(&project_id)?
                    .is_some_and(|r| r.token == token && !r.stopped)
            {
                owned.finish_receiver_job(&project_id, &token, false)?;
                owned.fail_runtime(&project_id, &error.to_string()).await?;
            }
            result
        });
        if spawned.is_err() {
            self.finish_receiver_job(id, &r.token, false)?;
        }
        spawned
    }

    pub(crate) fn finish_receiver_job(&self, id: &str, token: &str, completed: bool) -> Result<()> {
        self.update_runtime(id, |current| {
            if current.token == token
                && let Some(ReceiverWork::Check(band)) = current.receiver_work.take()
                && completed
            {
                current.receiver_checks_done.insert(band.band_id);
            }
            Ok(())
        })?;
        self.wake.notify_waiters();
        Ok(())
    }
    async fn investigate(
        self: &Arc<Self>,
        id: &str,
        event: Value,
        r: ProjectRuntime,
        dispatch: Arc<EventDispatch>,
    ) -> Result<()> {
        let event_id = s(&event, "id")?.to_owned();
        let p = self.project(id).await?;
        let target_frequency = Self::receiver_target(&event)?;
        let mode = p
            .get("receiver_mode")
            .and_then(Value::as_str)
            .unwrap_or("AUTO")
            .to_owned();
        let context = Arc::new(TuneContext {
            target: Self::target(&r, &r.receiver),
            permit: self.permit(id, &r),
            dispatch,
            state: Mutex::new(TuneState::default()),
        });
        let mut investigation = None;
        let mut observations = Vec::new();
        let result = async {
            self.check_job(id, &r.token, r.epoch, &r.receiver)?;
            let references = event["source_refs"]
                .as_array()
                .ok_or_else(|| invalid("event source_refs"))?
                .iter()
                .filter(|source| source["session_id"] == r.sid && source["stream_id"] == r.analyzer)
                .map(|source| s(source, "sweep_id"))
                .collect::<Result<Vec<_>>>()?;
            let candidate = self.sessions.create_candidate(&r.sid, json!({
                "source_stream_id": r.analyzer,
                "interval": {"started_at":event["first_seen"], "ended_at":event["last_seen"]},
                "center_frequency_hz": event["center_frequency_hz"],
                "peak_level": event["peak_level"],
                "level_unit": "dBm",
                "observation_ids": references,
            })).await.map_err(domain_error)?;
            let queued = self.sessions.enqueue_investigation(&r.sid, s(&candidate, "id")?, json!({
                "target_stream_id": r.receiver,
                "target_frequency_hz": target_frequency,
                "mode": mode,
                "priority": 1,
            })).await.map_err(domain_error)?;
            let job_id = s(&queued, "id")?.to_owned();
            investigation = Some(queued);
            self.check_job(id, &r.token, r.epoch, &r.receiver)?;
            let effects = Arc::new(GuardEffects {
                guard: self.weak.clone(),
                control: self.control.clone(),
                context: Some(context.clone()),
            });
            let listening = self.sessions.start_investigation_with_effects(&r.sid, &job_id, effects)
                .await.map_err(domain_error)?;
            if listening["state"] != "listening" {
                return Err(GuardError::Conflict(listening["error"].as_str()
                    .unwrap_or("Настройка не подтверждена").into()));
            }
            let (check, tune_reply) = {
                let state = context.state.lock().map_err(|_| ControlError::Denied)?;
                (
                    state.check.clone().ok_or(ControlError::Denied)?,
                    state.original_reply.clone().ok_or(ControlError::Denied)?,
                )
            };
            self.check_job(id, &r.token, r.epoch, &r.receiver)?;
            let tuned_at = tune_reply.acknowledged_at.clone();
            let seconds = number(&p["parameters"], "dwell_seconds")?;
            if !(1.0..=30.).contains(&seconds) {
                return Err(invalid("dwell_seconds должен быть 1..30"));
            }
            let deadline = self.clock.monotonic() + Duration::from_secs_f64(seconds);
            let mut artifact = None;
            let mut audio_error = Some("Аудиоисточник не выбран оператором".to_owned());
            if let Some(config) = p.get("audio").filter(|v| !v.is_null()) {
                let captured = async {
                    let devices = self.control.audio_inputs().await?;
                    if devices.available != Some(true) {
                        return Err(ControlError::Unavailable("audio_inputs").into());
                    }
                    let device = integer(config, "device")?;
                    let name = s(config, "name")?;
                    let input = devices.devices.into_iter()
                        .find(|d| u64::from(d.index) == device && d.name == name)
                        .ok_or_else(|| invalid("Назначенный аудиовход исчез или изменился"))?;
                    self.check_job(id, &r.token, r.epoch, &r.receiver)?;
                    check.revalidate().map_err(domain_error)?;
                    let raw = self.control.record_audio(AudioRequest {
                        target: context.target.clone(),
                        permit: context.permit.clone(),
                        tune_reply: tune_reply.clone(),
                        input: input.clone(),
                        seconds,
                        sample_rate_hz: 24000,
                    }).await?;
                    self.check_job(id, &r.token, r.epoch, &r.receiver)?;
                    if raw.wav_bytes.len() > 32 * 1024 * 1024 {
                        return Err(invalid("audio artifact size"));
                    }
                    let checksum = Sha256::digest(&raw.wav_bytes).iter()
                        .map(|byte| format!("{byte:02x}"))
                        .collect::<String>();
                    let item = Artifact {
                        id: self.ids.next_id(IdKind::Artifact),
                        session_id: r.sid.clone(),
                        stream_id: r.receiver.clone(),
                        schema_version: 1,
                        kind: "audio".into(),
                        mime_type: "audio/wav".into(),
                        filename: "ardv1-event.wav".into(),
                        sha256: checksum,
                        created_at: self.clock.now().as_str().into(),
                        payload: raw.wav_bytes,
                        metadata: json!({
                            "occurred_at": tuned_at,
                            "ended_at": self.clock.now(),
                            "event_id": event_id,
                            "investigation_id": job_id,
                            "frequency_hz": target_frequency,
                            "detected_frequency_hz": event["center_frequency_hz"],
                            "requested_mode": mode,
                            "source": "core_analog_input",
                            "device_name": input.name,
                            "capture_started_at": raw.started_at,
                            "capture_ended_at": raw.ended_at,
                            "sample_rate_hz": raw.sample_rate_hz,
                            "channels": raw.channels,
                        }),
                    };
                    let _tx = self.coordinator.lock().await;
                    self.check_job(id, &r.token, r.epoch, &r.receiver)?;
                    check.revalidate().map_err(domain_error)?;
                    let batch = self.archive.apply(vec![Mutation::Artifact(item)]).await?;
                    batch.items.into_iter().next().ok_or_else(|| invalid("artifact summary"))
                }.await;
                match captured {
                    Ok(saved) => {
                        artifact = Some(saved);
                        audio_error = None;
                    }
                    Err(error) => audio_error = Some(error.to_string()),
                }
            }
            while self.clock.monotonic() < deadline {
                self.check_job(id, &r.token, r.epoch, &r.receiver)?;
                tokio::select! {
                    _ = tokio::time::sleep(Duration::from_millis(50)) => {},
                    _ = self.wake.notified() => {},
                }
            }
            self.check_job(id, &r.token, r.epoch, &r.receiver)?;
            let ended_at = self.clock.now();
            let matcher = ReceiverMatch::new(&tune_reply, &p);
            observations = self.archive.list_observations(&r.sid, Some(&r.receiver), 1000)
                .await?
                .into_iter()
                .filter(|sample| matcher.as_ref().is_some_and(|m| m.interval(sample, &ended_at).is_some()))
                .collect();
            let state = if observations.is_empty() {
                self.sessions.fail_investigation(&r.sid, &job_id, "Нет наблюдений после подтверждённой настройки")
                    .await.map_err(domain_error)?;
                "no_observations"
            } else {
                let verified = observations.iter().filter_map(|row| row["id"].as_str()).collect();
                self.sessions.complete_verified_investigation(&r.sid, &job_id, check, &verified)
                    .await.map_err(domain_error)?;
                if observations.iter().any(|o| o["squelch_open"] == true) {
                    "observed"
                } else {
                    "not_present_at_receiver"
                }
            };
            let classifications = observations.iter()
                .filter_map(|o| {
                    let mode = o["decoded_mode"].as_str().filter(|s| !s.is_empty())?;
                    Some(json!({
                        "mode": mode,
                        "observation_id": o["id"],
                        "source": o["metadata"].get("decoder_source").cloned().unwrap_or(json!("simulated")),
                    }))
                })
                .collect::<Vec<_>>();
            Ok::<Value, GuardError>(json!({
                "state": state,
                "investigation_id": job_id,
                "requested_mode": mode,
                "target_frequency_hz": target_frequency,
                "detected_frequency_hz": event["center_frequency_hz"],
                "tuned_at": tuned_at,
                "ended_at": ended_at,
                "audio": artifact,
                "audio_error": audio_error,
                "observation_ids": observations.iter().map(|o| &o["id"]).collect::<Vec<_>>(),
                "receiver_status": observations.iter().skip(observations.len().saturating_sub(10)).collect::<Vec<_>>(),
                "classifications": classifications,
            }))
        }.await;
        let mut analysis = match result {
            Ok(analysis) => analysis,
            Err(error) => {
                let interrupted = self.check_job(id, &r.token, r.epoch, &r.receiver).is_err()
                    || !self.receiver_session_running(&r).await?;
                if let Some(job) = &investigation {
                    let _ = self
                        .sessions
                        .fail_investigation(&r.sid, s(job, "id")?, &error.to_string())
                        .await;
                }
                json!({
                    "state": if interrupted { "interrupted" } else { "failed" },
                    "error": error.to_string(),
                    "investigation_id": investigation.as_ref().and_then(|v| v["id"].as_str()),
                })
            }
        };
        analysis["event_age_seconds"] = event["analysis"]["event_age_seconds"].clone();
        let _tx = self.coordinator.lock().await;
        let mut current = self.event_record(id, &event_id).await?;
        let receipt = context
            .state
            .lock()
            .map_err(|_| ControlError::Denied)?
            .original_reply
            .clone();
        if context.dispatch.rejection(self.clock.monotonic()) == Some("event_retargeted")
            && current["disposition"] == "unreviewed"
            && self.check_job(id, &r.token, r.epoch, &r.receiver).is_ok()
        {
            if let Some(object) = current.as_object_mut() {
                object.remove("analysis");
            }
            self.archive.put_monitoring("event", current).await?;
            return self.finish_receiver_job(id, &r.token, true);
        }
        let reason = if analysis["state"] == "failed" {
            "investigation_failed"
        } else if analysis["state"] == "interrupted" {
            "control_context_revoked"
        } else {
            "missing_receiver_readback"
        };
        let reason = if receipt.is_none()
            && let Some(rejection) = context.dispatch.rejection(self.clock.monotonic())
        {
            analysis["state"] = json!(if rejection == "event_expired" {
                "expired"
            } else {
                "interrupted"
            });
            if let Some(object) = analysis.as_object_mut() {
                object.remove("error");
            }
            rejection
        } else {
            reason
        };
        verification(
            &mut analysis,
            "inconclusive",
            reason,
            Value::Null,
            Value::Null,
            None,
        );
        if !observations.is_empty()
            && !matches!(
                analysis["state"].as_str(),
                Some("failed" | "interrupted" | "expired")
            )
            && let Some(receipt) = receipt.as_ref()
        {
            let still_bound = || {
                self.check_job(id, &r.token, r.epoch, &r.receiver).is_ok()
                    && self.control.validate_tune(receipt).is_ok()
            };
            let running = self.receiver_session_running(&r).await?;
            if running && still_bound() {
                let project = self.project(id).await?;
                self.verify_event_receipt(
                    &project,
                    &r,
                    &current,
                    receipt,
                    &observations,
                    &mut analysis,
                )
                .await?;
            }
            if !running || !still_bound() {
                verification(
                    &mut analysis,
                    "inconclusive",
                    "control_context_revoked",
                    Value::Null,
                    Value::Null,
                    None,
                );
            }
        }
        let recording_failed = p["audio"].is_object() && analysis["audio_error"].is_string();
        current["analysis"] = analysis;
        let mut changes = vec![];
        if recording_failed {
            changes.push(self.recording_fault(&p, &current)?);
        }
        changes.push(Self::mutation("event", current));
        self.archive.apply(changes).await?;
        self.finish_receiver_job(id, &r.token, true)
    }
    pub async fn event_audio(&self, id: &str, event: &str) -> Result<Artifact> {
        let event = self.event_record(id, event).await?;
        let reference = event["analysis"]
            .get("audio")
            .filter(|a| !a.is_null())
            .ok_or(GuardError::NotFoundContext("Для события нет аудиозаписи"))?;
        if !self
            .archive
            .monitoring_member(id, s(reference, "session_id")?)
            .await?
        {
            return Err(GuardError::NotFound);
        }
        let item = self
            .archive
            .get_artifact(
                s(reference, "session_id")?,
                s(reference, "stream_id")?,
                s(reference, "id")?,
            )
            .await?
            .ok_or(GuardError::NotFound)?;
        if Some(item.session_id.as_str()) != reference["session_id"].as_str()
            || Some(item.stream_id.as_str()) != reference["stream_id"].as_str()
            || item.kind != "audio"
            || item.mime_type != "audio/wav"
            || !self.archive.monitoring_member(id, &item.session_id).await?
        {
            return Err(GuardError::NotFound);
        }
        Ok(item)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn native_events_require_the_original_rx_receipt_and_raw_units() {
        let now: ori_contracts::UtcTimestamp = "2026-11-11T13:00:00.123456Z".parse().unwrap();
        let reply = TuneReply {
            acknowledged: Some(true),
            applied_tune_revision: 3,
            acknowledged_at: now.clone(),
            worker: WorkerStatus {
                session_id: "session".into(),
                stream_id: "radio".into(),
                state: "running".into(),
                alive: true,
                current: Default::default(),
                extra: Default::default(),
                worker_instance: Some("owned-receiver".into()),
            },
            tuned: [
                ("frequency_hz".into(), json!(145_500_000)),
                ("mode".into(), json!("FM")),
            ]
            .into(),
        };
        let sample = json!({
            "id":"sample", "session_id":"session", "stream_id":"radio", "frequency_hz":145_500_000,
            "mode":"FM", "level":104, "level_unit":"s_meter_count", "squelch_open":true,
            "interval":{"started_at":now, "ended_at":now},
            "metadata":{ "adapter":"ardv1-serial", "simulated":false, "frequency_readback":true,
                "frequency_source":"receiver_RX", "mode_source":"receiver_RX", "raw_squelch_status":3,
                "raw_receiver_status":"RX VFA RF0145.50000 ST010.00 MD0F0 LM1043",
                "receiver_tune_context":receiver_tune_context("owned-receiver", 3, "sample") }
        });
        let project = json!({"profile":{"receiver":{"settings":{"adapter":"ardv1-serial"}}}});
        let matcher = ReceiverMatch::new(&reply, &project).unwrap();
        assert!(matcher.interval(&sample, &now).is_some());
        for (field, bad) in [
            ("/metadata/frequency_source", json!("command_acknowledged")),
            ("/metadata/frequency_readback", json!(false)),
            ("/metadata/mode_source", json!("receiver_MD")),
            ("/metadata/simulated", json!(true)),
            ("/metadata/raw_squelch_status", json!(0)),
            ("/metadata/raw_receiver_status", json!("")),
            ("/level_unit", json!("dBm")),
            ("/level", json!(104.5)),
            ("/frequency_hz", json!(145_510_000)),
            ("/id", json!("another")),
        ] {
            let mut altered = sample.clone();
            *altered.pointer_mut(field).unwrap() = bad;
            assert!(matcher.interval(&altered, &now).is_none(), "{field}");
        }
    }
}

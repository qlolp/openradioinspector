use crate::{
    runtime::{JobKind, ProjectRuntime},
    util::*,
    *,
};
use ori_archive::{Artifact, Mutation};
use ori_contracts::{TimeInterval, UtcTimestamp};
use ori_session::{SessionEffects, SessionError, SessionFuture, TuneCompletionCheck, TuneReceipt};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    sync::{Arc, Mutex, Weak},
    time::Duration,
};

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

fn matches_tune(
    sample: &Value,
    receipt: &TuneReply,
    adapter: &str,
    ended_at: &UtcTimestamp,
) -> bool {
    let metadata = &sample["metadata"];
    let Some(instance) = receipt.worker.worker_instance.as_deref() else {
        return false;
    };
    let Some(observation_id) = sample["id"].as_str() else {
        return false;
    };
    let Some(level) = sample["level"].as_f64() else {
        return false;
    };
    let Some(open) = sample["squelch_open"].as_bool() else {
        return false;
    };
    if receipt.acknowledged != Some(true)
        || receipt.applied_tune_revision == 0
        || sample["session_id"] != receipt.worker.session_id
        || sample["stream_id"] != receipt.worker.stream_id
        || receipt.tuned.get("frequency_hz") != Some(&sample["frequency_hz"])
        || receipt.tuned.get("mode") != Some(&sample["mode"])
        || sample["level_unit"] != "s_meter_count"
        || !(0.0..=f64::from(u32::MAX)).contains(&level)
        || level.fract() != 0.0
        || metadata["adapter"] != adapter
        || metadata["replay"] == true
        || metadata["receiver_tune_context"]
            != receiver_tune_context(instance, receipt.applied_tune_revision, observation_id)
    {
        return false;
    }
    let verified = match adapter {
        "ardv1-serial" => {
            metadata["simulated"] == false
                && metadata["frequency_readback"] == true
                && metadata["frequency_source"] == "receiver_RX"
                && metadata["mode_source"] == "receiver_RX"
                && metadata["raw_receiver_status"]
                    .as_str()
                    .is_some_and(|status| !status.is_empty())
                && metadata["raw_squelch_status"]
                    .as_u64()
                    .is_some_and(|state| state <= 3 && open == (state != 0))
        }
        "ardv1-sim" => metadata["simulated"] == true,
        _ => false,
    };
    verified
        && decode::<TimeInterval>(&sample["interval"]).is_ok_and(|interval| {
            receipt.acknowledged_at <= interval.started_at
                && interval.started_at <= interval.ended_at
                && interval.ended_at <= *ended_at
        })
}

pub(crate) struct GuardEffects {
    pub guard: Weak<GuardService>,
    pub control: Arc<dyn InstrumentControl>,
    pub context: Option<Arc<TuneContext>>,
}
pub(crate) struct TuneContext {
    target: ControlTarget,
    permit: ControlPermit,
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
            guard
                .authorize_control(&context.target, Some(&context.permit))
                .map_err(session_error)?;
            let reply = self
                .control
                .tune(TuneRequest {
                    target: context.target.clone(),
                    permit: context.permit.clone(),
                    frequency_hz: request.frequency_hz,
                    mode: request.mode.unwrap_or_else(|| "AUTO".into()),
                })
                .await
                .map_err(session_error)?;
            if reply.acknowledged != Some(true) {
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
impl GuardService {
    pub fn receiver_target(event: &Value) -> Result<u64> {
        let frequency = integer(event, "center_frequency_hz")?;
        if !(100_000..=1_300_000_000).contains(&frequency) {
            return Err(invalid("Событие вне диапазона AR-DV1"));
        }
        Ok(((frequency + 5) / 10) * 10)
    }
    pub(crate) async fn queue_receiver(&self, id: &str) -> Result<()> {
        let r = self
            .runtime_snapshot(id)?
            .filter(|r| !r.stopped)
            .ok_or(ControlError::Denied)?;
        if r.receiver_busy
            || r.manual.contains_key(&r.receiver)
            || r.faults.contains_key(&r.receiver)
        {
            return Ok(());
        }
        let Some(mut event) = self
            .archive
            .pending_receiver_events(id, &r.sid)
            .await?
            .into_iter()
            .next()
        else {
            return Ok(());
        };
        let target = Self::receiver_target(&event);
        let _tx = self.coordinator.lock().await;
        self.ensure_open()?;
        let current = self
            .runtime_snapshot(id)?
            .filter(|current| {
                !current.stopped
                    && current.token == r.token
                    && current.epoch == r.epoch
                    && !current.receiver_busy
                    && !current.manual.contains_key(&current.receiver)
            })
            .ok_or(ControlError::Denied)?;
        let latest = self.get_record("event", s(&event, "id")?, Some(id)).await?;
        if latest["analysis"]
            .get("state")
            .is_some_and(|a| !a.is_null())
            || latest["disposition"] != "unreviewed"
        {
            return Ok(());
        }
        event = latest;
        if let Err(error) = target {
            event["analysis"] = json!({"state":"unsupported_frequency","reason":error.to_string()});
            self.archive.put_monitoring("event", event).await?;
            return Ok(());
        }
        let age = elapsed(s(&event, "last_seen")?, self.clock.now().as_str())?;
        if age > 30. {
            event["analysis"] = json!({"state":"expired","reason":"Сигнал исчез до перестройки; обнаружение сохранено"});
            self.archive.put_monitoring("event", event).await?;
            return Ok(());
        }
        let p = self.project(id).await?;
        event["analysis"] = json!({"state":"queued","requested_mode":p.get("receiver_mode").cloned().unwrap_or(json!("AUTO"))});
        self.archive.put_monitoring("event", event.clone()).await?;
        self.update_runtime(id, |runtime| {
            if runtime.token != r.token || runtime.epoch != r.epoch || runtime.stopped {
                return Err(ControlError::Denied.into());
            }
            runtime.receiver_busy = true;
            Ok(())
        })?;
        drop(_tx);
        let owned = self.weak.upgrade().ok_or(ControlError::Denied)?;
        let project_id = id.to_owned();
        let r = current;
        self.spawn_job(id, JobKind::Receiver, async move {
            let result = owned.investigate(&project_id, event, r).await;
            if let Err(error) = &result {
                owned.fail_runtime(&project_id, &error.to_string()).await?;
            }
            result
        })?;
        Ok(())
    }
    async fn investigate(
        self: &Arc<Self>,
        id: &str,
        event: Value,
        r: ProjectRuntime,
    ) -> Result<()> {
        let event_id = s(&event, "id")?.to_owned();
        let p = self.project(id).await?;
        let target_frequency = Self::receiver_target(&event)?;
        let mode = p
            .get("receiver_mode")
            .and_then(Value::as_str)
            .unwrap_or("AUTO")
            .to_owned();
        let mut investigation: Option<Value> = None;
        let result=async{
            self.check_job(id,&r.token,r.epoch,&r.receiver)?;
            let references=event["source_refs"].as_array().ok_or_else(||invalid("event source_refs"))?.iter().filter(|source|source["session_id"].as_str()==Some(&r.sid)).map(|source|s(source,"sweep_id").map(str::to_owned)).collect::<Result<Vec<_>>>()?;
            let candidate=self.sessions.create_candidate(&r.sid,json!({"source_stream_id":p["analyzer_stream_id"],"interval":{"started_at":event["first_seen"],"ended_at":event["last_seen"]},"center_frequency_hz":event["center_frequency_hz"],"peak_level":event["peak_level"],"level_unit":"dBm","observation_ids":references})).await.map_err(domain_error)?;
            let queued=self.sessions.enqueue_investigation(&r.sid,s(&candidate,"id")?,json!({"target_stream_id":r.receiver,"target_frequency_hz":target_frequency,"mode":mode,"priority":1})).await.map_err(domain_error)?;let job_id=s(&queued,"id")?.to_owned();investigation=Some(queued);
            self.check_job(id,&r.token,r.epoch,&r.receiver)?;let context=Arc::new(TuneContext{target:Self::target(&r,&r.receiver),permit:self.permit(id,&r),state:Mutex::new(TuneState::default())});let effects=Arc::new(GuardEffects{guard:self.weak.clone(),control:self.control.clone(),context:Some(context.clone())});let listening=self.sessions.start_investigation_with_effects(&r.sid,&job_id,effects).await.map_err(domain_error)?;investigation=Some(listening.clone());if listening["state"]!="listening"{return Err(GuardError::Conflict(listening["error"].as_str().unwrap_or("Настройка не подтверждена").into()));}
            self.check_job(id, &r.token, r.epoch, &r.receiver)?;
            let (check, tune_reply) = {
                let state = context.state.lock().map_err(|_| ControlError::Denied)?;
                (state.check.clone().ok_or(ControlError::Denied)?, state.original_reply.clone().ok_or(ControlError::Denied)?)
            };
            let tuned_at = &tune_reply.acknowledged_at;
            let seconds=number(&p["parameters"],"dwell_seconds")?;if !(1.0..=30.).contains(&seconds){return Err(invalid("dwell_seconds должен быть 1..30"));}let deadline=self.clock.monotonic()+Duration::from_secs_f64(seconds);let mut artifact=None;let mut audio_error=Some("Аудиоисточник не выбран оператором".to_owned());
            if let Some(config)=p.get("audio").filter(|v|!v.is_null()){
                let captured=async{
                    let devices=self.control.audio_inputs().await?;if devices.available!=Some(true){return Err(ControlError::Unavailable("audio_inputs").into());}let device=integer(config,"device")?;let name=s(config,"name")?;let input=devices.devices.into_iter().find(|d|u64::from(d.index)==device&&d.name==name).ok_or_else(||invalid("Назначенный аудиовход исчез или изменился"))?;self.check_job(id,&r.token,r.epoch,&r.receiver)?;
                    check.revalidate().map_err(domain_error)?;
                    let raw=self.control.record_audio(AudioRequest{target:Self::target(&r,&r.receiver),permit:self.permit(id,&r),tune_reply:tune_reply.clone(),input:input.clone(),seconds,sample_rate_hz:24000}).await?;self.check_job(id,&r.token,r.epoch,&r.receiver)?;if raw.wav_bytes.len()>32*1024*1024{return Err(invalid("audio artifact size"));}
                    let checksum=Sha256::digest(&raw.wav_bytes).iter().map(|byte|format!("{byte:02x}")).collect::<String>();
                    let item=Artifact{id:self.ids.next_id(IdKind::Artifact),session_id:r.sid.clone(),stream_id:r.receiver.clone(),schema_version:1,kind:"audio".into(),mime_type:"audio/wav".into(),filename:"ardv1-event.wav".into(),sha256:checksum,created_at:self.clock.now().as_str().into(),payload:raw.wav_bytes,metadata:json!({"occurred_at":tuned_at.as_str(),"ended_at":self.clock.now().as_str(),"event_id":event_id,"investigation_id":job_id,"frequency_hz":target_frequency,"detected_frequency_hz":event["center_frequency_hz"],"requested_mode":mode,"source":"core_analog_input","device_name":input.name,"capture_started_at":raw.started_at.as_str(),"capture_ended_at":raw.ended_at.as_str(),"sample_rate_hz":raw.sample_rate_hz,"channels":raw.channels})};
                    let _tx=self.coordinator.lock().await;self.check_job(id,&r.token,r.epoch,&r.receiver)?;check.revalidate().map_err(domain_error)?;let batch=self.archive.apply(vec![Mutation::Artifact(item)]).await?;batch.items.into_iter().next().ok_or_else(||invalid("artifact summary"))
                }.await;
                match captured{Ok(saved)=>{artifact=Some(saved);audio_error=None;},Err(error)=>audio_error=Some(error.to_string())}
            }
            while self.clock.monotonic()<deadline{self.check_job(id,&r.token,r.epoch,&r.receiver)?;tokio::select!{_ = tokio::time::sleep(Duration::from_millis(50))=>{},_ = self.wake.notified()=>{}}}
            self.check_job(id, &r.token, r.epoch, &r.receiver)?;
            let ended_at = self.clock.now();
            let adapter = s(&p["profile"]["receiver"]["settings"], "adapter")?;
            let observations = self.archive.list_observations(&r.sid, Some(&r.receiver), 1000)
                .await?.into_iter()
                .filter(|sample| matches_tune(sample, &tune_reply, adapter, &ended_at))
                .collect::<Vec<_>>();
            let state = if observations.is_empty() {
                self.sessions.fail_investigation(&r.sid, &job_id, "Нет наблюдений после подтверждённой настройки")
                    .await.map_err(domain_error)?;
                "no_observations"
            } else {
                let verified = observations.iter().filter_map(|row| row["id"].as_str()).collect();
                self.sessions.complete_verified_investigation(&r.sid, &job_id, check, &verified)
                    .await.map_err(domain_error)?;
                if observations.iter().any(|row| row["squelch_open"] == true) { "observed" } else { "not_present_at_receiver" }
            };
            let classifications=observations.iter().filter_map(|o|o["decoded_mode"].as_str().filter(|s|!s.is_empty()).map(|mode|json!({"mode":mode,"observation_id":o["id"],"source":o["metadata"].get("decoder_source").cloned().unwrap_or(json!("simulated"))}))).collect::<Vec<_>>();
            Ok::<Value,GuardError>(json!({"state":state,"investigation_id":job_id,"requested_mode":mode,"target_frequency_hz":target_frequency,"detected_frequency_hz":event["center_frequency_hz"],"tuned_at":tuned_at.as_str(),"ended_at":self.clock.now().as_str(),"audio":artifact,"audio_error":audio_error,"observation_ids":observations.iter().map(|o|o["id"].clone()).collect::<Vec<_>>(),"receiver_status":observations.iter().skip(observations.len().saturating_sub(10)).cloned().collect::<Vec<_>>(),"classifications":classifications}))
        }.await;
        let result = match result {
            Ok(result) => result,
            Err(error) => {
                let interrupted = self.closed.load(std::sync::atomic::Ordering::Acquire)
                    || self.runtime_snapshot(id)?.is_none_or(|current| {
                        current.stopped || current.epoch != r.epoch || current.token != r.token
                    });
                if let Some(job) = &investigation {
                    let _ = self
                        .sessions
                        .fail_investigation(&r.sid, s(job, "id")?, &error.to_string())
                        .await;
                }
                json!({"state":if interrupted{"interrupted"}else{"failed"},"error":error.to_string(),"investigation_id":investigation.as_ref().and_then(|v|v["id"].as_str())})
            }
        };
        let _tx = self.coordinator.lock().await;
        let mut current = self.get_record("event", &event_id, Some(id)).await?;
        current["analysis"] = result;
        self.archive.put_monitoring("event", current).await?;
        self.update_runtime(id, |current| {
            if current.token == r.token {
                current.receiver_busy = false;
            }
            Ok(())
        })?;
        Ok(())
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
        let now: UtcTimestamp = "2026-11-11T13:00:00.123456Z".parse().unwrap();
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
        assert!(matches_tune(&sample, &reply, "ardv1-serial", &now));
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
            assert!(
                !matches_tune(&altered, &reply, "ardv1-serial", &now),
                "{field}"
            );
        }
    }
}

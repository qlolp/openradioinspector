//! Thin domain/effect binding. Domain crates own business transactions; Core
//! alone owns device actors, filesystem probes, HTTP and research processes.
use crate::{
    Core, CoreError,
    devices::{DeviceError, ExecutionGate, Target, TuneOutcome},
    error::{ApiError, ApiResult},
};
use ori_contracts::{Session, UtcTimestamp, parse_json_value, to_json_vec};
use ori_guard::{
    ControlError, ControlFuture, ControlPermit, ControlResult, ControlTarget, GuardService,
    GuardShutdown, InstrumentControl,
};
use ori_session::{
    SessionEffects, SessionError, SessionFuture, SessionService, TuneCompletionCheck, TuneReceipt,
};
use serde_json::Value;
use std::{
    sync::{Arc, OnceLock, Weak},
    time::{Duration, Instant},
};

/// Shared domain clock/identifiers for deterministic integration fixtures.
/// Driver-acquired raw timestamps are never rewritten with this clock.
#[derive(Clone)]
pub struct DomainSources {
    pub clock: Arc<dyn ori_guard::Clock>,
    pub ids: Arc<dyn ori_guard::IdSource>,
}
struct Clock {
    start: Instant,
}
impl ori_guard::Clock for Clock {
    fn now(&self) -> UtcTimestamp {
        crate::utc_now().parse().expect("system UTC")
    }
    fn monotonic(&self) -> Duration {
        self.start.elapsed()
    }
}
struct Ids;
impl ori_guard::IdSource for Ids {
    fn next_id(&self, kind: ori_guard::IdKind) -> String {
        let mut value = crate::new_id();
        let mut bytes = value.into_bytes();
        bytes[12] = b'4';
        bytes[16] = b'8';
        value = String::from_utf8(bytes).expect("ASCII ID");
        if matches!(kind, ori_guard::IdKind::Record | ori_guard::IdKind::Owner) {
            value
        } else {
            format!(
                "{}-{}-{}-{}-{}",
                &value[..8],
                &value[8..12],
                &value[12..16],
                &value[16..20],
                &value[20..]
            )
        }
    }
}
pub(crate) struct Domains {
    pub session: Arc<SessionService>,
    pub guard: Arc<GuardService>,
    runtime: tokio::runtime::Handle,
    shutdown: OnceLock<std::result::Result<GuardShutdown, String>>,
}
impl Domains {
    pub fn begin_shutdown(&self) -> std::result::Result<GuardShutdown, String> {
        self.shutdown
            .get_or_init(|| {
                // EOF lifelines and embeddings may run on an owned standard
                // thread. Enter this Core's runtime only for the synchronous
                // begin call; retain the same owned cleanup attempt.
                let _entered = self.runtime.enter();
                self.guard
                    .begin_shutdown()
                    .map_err(|error| error.to_string())
            })
            .clone()
    }
}
pub(crate) async fn initialize(core: &Arc<Core>) -> crate::Result<()> {
    let Some(archive) = core.archive.as_ref() else {
        return Ok(());
    };
    let sources = core
        .config
        .domain_sources
        .clone()
        .unwrap_or_else(|| DomainSources {
            clock: Arc::new(Clock {
                start: Instant::now(),
            }),
            ids: Arc::new(Ids),
        });
    let effects = Arc::new(Effects {
        core: Arc::downgrade(core),
    });
    let clock = sources.clock.clone();
    let ids = sources.ids.clone();
    let session = SessionService::with_sources(
        archive.clone(),
        effects.clone(),
        Arc::new(move || {
            chrono::DateTime::from_timestamp_micros(clock.now().unix_micros())
                .expect("typed domain UTC")
        }),
        Arc::new(move || ids.next_id(ori_guard::IdKind::Investigation)),
    );
    let guard = GuardService::with_sources(archive.clone(), effects, sources.clock, sources.ids);
    let policy = Arc::downgrade(&guard);
    core.devices
        .as_ref()
        .expect("replay device manager")
        .attach_public_policy(Arc::new(move |target| {
            policy
                .upgrade()
                .ok_or(DeviceError::Closed)?
                .authorize_control(
                    &ControlTarget {
                        session_id: target.session_id.clone(),
                        stream_id: target.stream_id.clone(),
                    },
                    None,
                )
                .map_err(|_| DeviceError::Denied)
        }))?;
    core.domains
        .set(Domains {
            session,
            guard: guard.clone(),
            runtime: tokio::runtime::Handle::current(),
            shutdown: OnceLock::new(),
        })
        .map_err(|_| CoreError::Invalid("domains already initialized"))?;
    guard.start().await?;
    Ok(())
}
impl Core {
    pub(crate) fn domain(&self) -> ApiResult<&Domains> {
        self.archive()?;
        self.domains
            .get()
            .ok_or_else(|| ApiError::unavailable("domain_startup_incomplete"))
    }
    /// Existing same-attempt intent barrier for owned native cleanup.
    pub(crate) async fn control_stop_barrier(&self) -> crate::Result<()> {
        if (*self.shutdown.borrow() || self.admission.is_quiescing())
            && let Some(domain) = self.domains.get()
        {
            domain
                .begin_shutdown()
                .map_err(|_| CoreError::Invalid("guard shutdown intent could not start"))?
                .intent_persisted()
                .await?;
        }
        Ok(())
    }
    /// Persistence barrier is required before hardware cancellation. Even on
    /// failure, quiesce owned effects, drain the same attempt, then report the
    /// error so the external writer lease remains held.
    pub(crate) async fn drain_domains(&self) -> crate::Result<()> {
        let Some(domain) = self.domains.get() else {
            if let Some(control) = self.instrument_control.get() {
                control.begin_shutdown().completed().await?;
            }
            if let Some(devices) = &self.devices {
                devices.request_shutdown()?;
            }
            return Ok(());
        };
        let handle = match domain.begin_shutdown() {
            Ok(handle) => handle,
            Err(_) => {
                if let Some(devices) = &self.devices {
                    let _ = devices.request_shutdown();
                }
                return Err(CoreError::Invalid(
                    "guard shutdown could not establish durable intent",
                ));
            }
        };
        let intent = handle.intent_persisted().await;
        let controlled = match self.instrument_control.get() {
            Some(control) => control.begin_shutdown().completed().await,
            None => Ok(()),
        };
        let stopped = self
            .devices
            .as_ref()
            .map(|devices| devices.request_shutdown())
            .transpose();
        let complete = handle.completed().await;
        intent?;
        controlled?;
        stopped?;
        complete?;
        Ok(())
    }
}
struct Effects {
    core: Weak<Core>,
}
fn unavailable() -> ControlError {
    ControlError::Unavailable("core_lifecycle")
}
fn audio_error(error: ori_audio::AudioError) -> ControlError {
    match error {
        ori_audio::AudioError::Revoked
        | ori_audio::AudioError::Cancelled
        | ori_audio::AudioError::Closed => ControlError::Denied,
        ori_audio::AudioError::Disabled | ori_audio::AudioError::NativeUnavailable => {
            ControlError::Unavailable("native_audio_disabled")
        }
        error => ControlError::Failed(error.to_string()),
    }
}
fn control(error: DeviceError) -> ControlError {
    match error {
        DeviceError::Denied | DeviceError::Cancelled | DeviceError::Closed => ControlError::Denied,
        error => ControlError::Failed(error.to_string()),
    }
}
fn session_error(error: DeviceError) -> SessionError {
    let error = ApiError::from(error);
    SessionError::new(error.status.as_u16(), &error.code, error.message)
}
fn gate(
    core: &Core,
    target: &ControlTarget,
    permit: ControlPermit,
) -> ControlResult<ExecutionGate> {
    let guard = Arc::downgrade(&core.domains.get().ok_or_else(unavailable)?.guard);
    let target = target.clone();
    let expected = permit.expected_worker_instance().map(str::to_owned);
    Ok(ExecutionGate::new(
        move || {
            guard
                .upgrade()
                .ok_or(DeviceError::Closed)?
                .authorize_control(&target, Some(&permit))
                .map_err(|_| DeviceError::Denied)
        },
        expected,
    ))
}
fn worker(status: crate::devices::WorkerStatus) -> ControlResult<ori_guard::WorkerStatus> {
    let mut extra = status
        .value()
        .map_err(control)?
        .as_object()
        .cloned()
        .ok_or_else(unavailable)?;
    for key in ["session_id", "stream_id", "state", "alive", "current"] {
        extra.remove(key);
    }
    Ok(ori_guard::WorkerStatus {
        session_id: status.session_id,
        stream_id: status.stream_id,
        state: status.state,
        alive: status.alive,
        current: status
            .current
            .as_object()
            .ok_or_else(unavailable)?
            .iter()
            .map(|(key, value)| (key.clone(), value.clone()))
            .collect(),
        extra: extra.into_iter().collect(),
        worker_instance: status.worker_instance,
    })
}
/// Only called with a successful stop receipt, after the manager has awaited
/// this worker's cleanup and join. Public snapshots keep `disconnected`.
fn joined_worker(status: crate::devices::WorkerStatus) -> ControlResult<ori_guard::WorkerStatus> {
    if status.alive {
        return Err(ControlError::Failed("worker stop did not finish".into()));
    }
    let mut stopped = worker(status)?;
    stopped.state = "stopped".into();
    Ok(stopped)
}
struct Completion(TuneOutcome);
impl TuneCompletionCheck for Completion {
    fn revalidate(&self) -> ori_session::SessionResult<()> {
        self.0.revalidate().map_err(session_error)
    }
}
impl SessionEffects for Effects {
    fn tune(&self, request: ori_session::TuneRequest) -> SessionFuture<'_, TuneReceipt> {
        Box::pin(async move {
            let core = self.core.upgrade().ok_or_else(|| {
                SessionError::new(503, "worker_unavailable", "Core is unavailable")
            })?;
            let devices = core.devices.as_ref().ok_or_else(|| {
                SessionError::new(503, "worker_unavailable", "Device registry is unavailable")
            })?;
            let target = Target::new(&request.session_id, &request.stream_id);
            let result = devices
                .tune(
                    target.clone(),
                    request.frequency_hz,
                    request.mode,
                    devices.public_gate(target),
                )
                .await
                .map_err(session_error)?;
            Ok(TuneReceipt::new(Arc::new(Completion(result))))
        })
    }
    fn validate_reference(&self, session: Value, sweep: Value) -> SessionFuture<'_, ()> {
        Box::pin(async move {
            let core = self.core.upgrade().ok_or_else(|| {
                SessionError::new(503, "research_unavailable", "Core is unavailable")
            })?;
            crate::analysis::validate_reference(&core, session, sweep)
                .await
                .map_err(|error| {
                    SessionError::new(error.status.as_u16(), &error.code, error.message)
                })
        })
    }
}
impl InstrumentControl for Effects {
    fn preflight<'a>(
        &'a self,
        session: &'a Session,
    ) -> ControlFuture<'a, ori_guard::InstrumentPreflight> {
        Box::pin(async move {
            let core = self.core.upgrade().ok_or_else(unavailable)?;
            let devices = core.devices.as_ref().ok_or_else(unavailable)?;
            let checked = devices.validate_profiles(session).map_err(control);
            let check=ori_guard::PreflightCheck{name:"core_adapter_configuration".into(),confirmed:Some(checked.is_ok()),detail:checked.as_ref().err().map(ToString::to_string).unwrap_or_else(||"Explicit supported profiles; physical hardware validation not run. Native start must await safe-prepare acknowledgement.".into())};
            Ok(ori_guard::InstrumentPreflight {
                ready: Some(checked.is_ok()),
                checks: vec![check],
            })
        })
    }
    fn list_workers<'a>(
        &'a self,
        session: &'a str,
    ) -> ControlFuture<'a, Vec<ori_guard::WorkerStatus>> {
        Box::pin(async move {
            let core = self.core.upgrade().ok_or_else(unavailable)?;
            core.devices
                .as_ref()
                .ok_or_else(unavailable)?
                .list(Some(session))
                .await
                .map_err(control)?
                .into_iter()
                .map(worker)
                .collect()
        })
    }
    fn start_session<'a>(
        &'a self,
        session: &'a Session,
        permit: ControlPermit,
    ) -> ControlFuture<'a, Vec<ori_guard::WorkerStatus>> {
        Box::pin(async move {
            let core = self.core.upgrade().ok_or_else(unavailable)?;
            let guard = Arc::downgrade(&core.domains.get().ok_or_else(unavailable)?.guard);
            let expected = permit.expected_worker_instance().map(str::to_owned);
            let factory = Arc::new(move |target: &Target| {
                let guard = guard.clone();
                let permit = permit.clone();
                let target = ControlTarget {
                    session_id: target.session_id.clone(),
                    stream_id: target.stream_id.clone(),
                };
                ExecutionGate::new(
                    move || {
                        guard
                            .upgrade()
                            .ok_or(DeviceError::Closed)?
                            .authorize_control(&target, Some(&permit))
                            .map_err(|_| DeviceError::Denied)
                    },
                    expected.clone(),
                )
            });
            core.devices
                .as_ref()
                .ok_or_else(unavailable)?
                .start_session_gated(&session.id, factory)
                .await
                .map_err(control)?
                .into_iter()
                .map(worker)
                .collect()
        })
    }
    fn stop_session<'a>(
        &'a self,
        session: &'a str,
    ) -> ControlFuture<'a, Vec<ori_guard::WorkerStatus>> {
        Box::pin(async move {
            let core = self.core.upgrade().ok_or_else(unavailable)?;
            if let Some(control_service) = core.instrument_control.get() {
                for status in core
                    .devices
                    .as_ref()
                    .ok_or_else(unavailable)?
                    .list(Some(session))
                    .await
                    .map_err(control)?
                {
                    control_service
                        .close_target(&Target::new(&status.session_id, &status.stream_id))
                        .await
                        .map_err(control)?;
                }
            }
            core.devices
                .as_ref()
                .ok_or_else(unavailable)?
                .stop_session(session)
                .await
                .map_err(control)?
                .into_iter()
                .map(joined_worker)
                .collect()
        })
    }
    fn start_stream<'a>(
        &'a self,
        session: &'a Session,
        stream: &'a str,
        permit: ControlPermit,
    ) -> ControlFuture<'a, ori_guard::WorkerStatus> {
        Box::pin(async move {
            let core = self.core.upgrade().ok_or_else(unavailable)?;
            let target = ControlTarget {
                session_id: session.id.clone(),
                stream_id: stream.into(),
            };
            let gate = gate(&core, &target, permit)?;
            worker(
                core.devices
                    .as_ref()
                    .ok_or_else(unavailable)?
                    .start_stream(&session.id, stream, gate)
                    .await
                    .map_err(control)?,
            )
        })
    }
    fn stop_stream<'a>(
        &'a self,
        target: &'a ControlTarget,
    ) -> ControlFuture<'a, ori_guard::WorkerStatus> {
        Box::pin(async move {
            let core = self.core.upgrade().ok_or_else(unavailable)?;
            if let Some(control_service) = core.instrument_control.get() {
                control_service
                    .close_target(&Target::new(&target.session_id, &target.stream_id))
                    .await
                    .map_err(control)?;
            }
            joined_worker(
                core.devices
                    .as_ref()
                    .ok_or_else(unavailable)?
                    .stop_stream(&target.session_id, &target.stream_id)
                    .await
                    .map_err(control)?,
            )
        })
    }
    fn tune<'a>(
        &'a self,
        request: ori_guard::TuneRequest,
    ) -> ControlFuture<'a, ori_guard::TuneReply> {
        Box::pin(async move {
            let core = self.core.upgrade().ok_or_else(unavailable)?;
            let execution = gate(&core, &request.target, request.permit)?;
            let result = core
                .devices
                .as_ref()
                .ok_or_else(unavailable)?
                .tune(
                    Target::new(&request.target.session_id, &request.target.stream_id),
                    request.frequency_hz,
                    Some(request.mode),
                    execution,
                )
                .await
                .map_err(control)?;
            Ok(ori_guard::TuneReply {
                acknowledged: Some(true),
                tuned: result
                    .tuned
                    .as_object()
                    .ok_or_else(unavailable)?
                    .iter()
                    .map(|(k, v)| (k.clone(), v.clone()))
                    .collect(),
                worker: worker(result.worker)?,
                acknowledged_at: result.acknowledged_at.parse().map_err(|_| unavailable())?,
                applied_tune_revision: result.applied_tune_revision,
            })
        })
    }
    fn validate_tune(&self, reply: &ori_guard::TuneReply) -> ControlResult<()> {
        let core = self.core.upgrade().ok_or_else(unavailable)?;
        let target = Target::new(&reply.worker.session_id, &reply.worker.stream_id);
        let instance = reply
            .worker
            .worker_instance
            .as_deref()
            .ok_or(ControlError::Denied)?;
        let bytes = to_json_vec(&reply.tuned).map_err(|_| unavailable())?;
        let tuned = parse_json_value(&bytes, 65536).map_err(|_| unavailable())?;
        core.devices
            .as_ref()
            .ok_or_else(unavailable)?
            .validate_tune(&target, instance, reply.applied_tune_revision, &tuned)
            .map_err(control)
    }
    fn audio_inputs(&self) -> ControlFuture<'_, ori_guard::AudioInputs> {
        Box::pin(async move {
            let core = self.core.upgrade().ok_or_else(unavailable)?;
            let Some(audio) = &core.audio else {
                return Ok(ori_guard::AudioInputs {
                    available: Some(false),
                    devices: Vec::new(),
                    error: Some("Native audio is disabled".into()),
                });
            };
            let devices = audio.inputs().await.map_err(audio_error)?;
            Ok(ori_guard::AudioInputs {
                available: Some(!devices.is_empty()),
                devices,
                error: None,
            })
        })
    }
    fn record_audio(
        &self,
        request: ori_guard::AudioRequest,
    ) -> ControlFuture<'_, ori_guard::RecordedAudio> {
        // Capture these ORIGINAL receipts before the first await. Each block
        // validates them without filesystem, archive, actor or network work.
        let prepared = (|| {
            let core = self.core.upgrade().ok_or_else(unavailable)?;
            if !request.seconds.is_finite() || !(0.1..=30.0).contains(&request.seconds) {
                return Err(ControlError::Failed(
                    "Недопустимый формат аналоговой записи".into(),
                ));
            }
            let proof = crate::audio::TuneProof::new(&request.target, &request.tune_reply)
                .map_err(audio_error)?;
            let owner = gate(&core, &request.target, request.permit)?;
            Ok((core, proof, owner))
        })();
        Box::pin(async move {
            let (core, proof, owner) = prepared?;
            let recorded = core
                .audio
                .as_ref()
                .ok_or(ControlError::Unavailable("native_audio_disabled"))?
                .record(
                    &core,
                    ori_audio::InputSelection {
                        index: request.input.index,
                        name: request.input.name,
                    },
                    request.seconds,
                    request.sample_rate_hz,
                    proof,
                    owner,
                )
                .await
                .map_err(audio_error)?;
            Ok(ori_guard::RecordedAudio {
                wav_bytes: recorded.wav_bytes,
                started_at: recorded.started_at,
                ended_at: recorded.ended_at,
                sample_rate_hz: recorded.sample_rate_hz,
                channels: recorded.channels,
            })
        })
    }
    fn storage_probe(&self) -> ControlFuture<'_, ori_guard::StorageProbe> {
        Box::pin(async move {
            let core = self.core.upgrade().ok_or_else(unavailable)?;
            if *core.shutdown.borrow() {
                return Err(unavailable());
            }
            let directory = core.config.data_directory.clone();
            let admitted = core
                .effect_io
                .clone()
                .try_acquire_owned()
                .map_err(|_| ControlError::Unavailable("storage_probe_busy"))?;
            if *core.shutdown.borrow() {
                return Err(unavailable());
            }
            tokio::task::spawn_blocking(move || {
                let _held = admitted;
                let free = fs2::available_space(&directory).ok();
                let path = directory.join(format!(".ori-storage-probe-{}", crate::new_id()));
                let mut options = std::fs::OpenOptions::new();
                options.write(true).create_new(true);
                #[cfg(unix)]
                {
                    use std::os::unix::fs::OpenOptionsExt;
                    options.mode(0o600);
                }
                let writable = match options.open(&path) {
                    Ok(mut file) => {
                        use std::io::Write;
                        let result = file
                            .write_all(b"ori-storage-probe")
                            .and_then(|()| file.sync_all());
                        drop(file);
                        let removed = std::fs::remove_file(path);
                        Some(result.is_ok() && removed.is_ok())
                    }
                    Err(_) => Some(false),
                };
                ori_guard::StorageProbe {
                    filesystem_free_bytes: free,
                    writable,
                }
            })
            .await
            .map_err(|_| ControlError::Unavailable("storage_probe_failed"))
        })
    }
    fn deliver_webhook<'a>(
        &'a self,
        request: ori_guard::WebhookRequest,
    ) -> ControlFuture<'a, ori_guard::DeliveryReceipt> {
        Box::pin(async move {
            use hmac::{Hmac, KeyInit, Mac};
            use sha2::Sha256;
            let core = self.core.upgrade().ok_or_else(unavailable)?;
            let _admitted = core
                .webhook_slots
                .clone()
                .try_acquire_owned()
                .map_err(|_| ControlError::Unavailable("notification_busy"))?;
            let url = url::Url::parse(&request.url)
                .map_err(|_| ControlError::Failed("invalid webhook URL".into()))?;
            if !matches!(url.scheme(), "http" | "https")
                || !url.username().is_empty()
                || url.password().is_some()
            {
                return Err(ControlError::Failed("invalid webhook URL".into()));
            }
            let mut signature = Hmac::<Sha256>::new_from_slice(request.secret.as_bytes())
                .map_err(|_| ControlError::Failed("invalid webhook key".into()))?;
            signature.update(&request.body);
            let signature = signature
                .finalize()
                .into_bytes()
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect::<String>();
            let response = core
                .webhook_client
                .post(url)
                .header("Content-Type", "application/json")
                .header(
                    "X-OpenRadioInspector-Notification-ID",
                    request.notification_id,
                )
                .header(
                    "X-OpenRadioInspector-Signature",
                    format!("sha256={signature}"),
                )
                .body(request.body)
                .send()
                .await
                .map_err(|_| ControlError::Failed("network_error".into()))?;
            let status = response.status().as_u16();
            Ok(ori_guard::DeliveryReceipt {
                delivered: Some((200..300).contains(&status)),
                status: Some(status),
            })
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    #[tokio::test]
    async fn trusted_guard_stop_reports_joined_stopped_instance_without_changing_public_status() {
        let directory = tempfile::tempdir().unwrap();
        let core = Core::open(crate::CoreConfig::replay(directory.path()))
            .await
            .unwrap();
        core.archive().unwrap().create_session(serde_json::json!({
            "id":"joined","name":"Joined fixture","contract_version":1,"state":"running",
            "created_at":"2026-11-11T00:00:00.000000Z","started_at":"2026-11-11T00:00:00.000000Z","ended_at":null,"metadata":{},
            "streams":[
                {"id":"a","session_id":"joined","device":"Synthetic analyzer","role":"spectrum_analyzer","state":"disconnected","level_unit":"dBm","settings":{"adapter":"tinysa-sim","start_hz":100,"stop_hz":200,"points":3,"period_s":0.01}},
                {"id":"r","session_id":"joined","device":"Synthetic receiver","role":"receiver","state":"disconnected","level_unit":"s_meter_count","settings":{"adapter":"ardv1-sim","period_s":0.01}}
            ]
        })).await.unwrap();
        let manager = core.devices.as_ref().unwrap();
        let target = Target::new("joined", "a");
        let started = manager
            .start_stream("joined", "a", manager.public_gate(target))
            .await
            .unwrap();
        let effects = Effects {
            core: Arc::downgrade(&core),
        };
        let stopped = effects
            .stop_stream(&ControlTarget {
                session_id: "joined".into(),
                stream_id: "a".into(),
            })
            .await
            .unwrap();
        let public = manager
            .list(Some("joined"))
            .await
            .unwrap()
            .into_iter()
            .find(|status| status.stream_id == "a")
            .unwrap();
        for stream in ["a", "r"] {
            manager
                .start_stream(
                    "joined",
                    stream,
                    manager.public_gate(Target::new("joined", stream)),
                )
                .await
                .unwrap();
        }
        let all = effects.stop_session("joined").await.unwrap();
        let public_all = manager.list(Some("joined")).await.unwrap();
        core.close().await.unwrap();
        assert_eq!(stopped.state, "stopped");
        assert!(!stopped.alive);
        assert_eq!(stopped.worker_instance, started.worker_instance);
        assert_eq!(public.state, "disconnected");
        assert!(!public.alive);
        assert!(all.iter().all(|status| status.state == "stopped"
            && !status.alive
            && status.worker_instance.is_some()));
        assert!(
            public_all
                .iter()
                .all(|status| status.state == "disconnected" && !status.alive)
        );
    }

    #[tokio::test]
    async fn webhook_signs_exact_bytes_and_never_follows_redirects() {
        let directory = tempfile::tempdir().unwrap();
        let core = Core::open(crate::CoreConfig::replay(directory.path()))
            .await
            .unwrap();
        let sink = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let redirect = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}/notify", sink.local_addr().unwrap());
        let location = format!("http://{}/forbidden", redirect.local_addr().unwrap());
        let capture = tokio::spawn(async move {
            let (mut socket, _) = sink.accept().await.unwrap();
            let mut raw = Vec::new();
            let split = loop {
                let byte = socket.read_u8().await.unwrap();
                raw.push(byte);
                assert!(raw.len() < 8192);
                if raw.ends_with(b"\r\n\r\n") {
                    break raw.len();
                }
            };
            let head = String::from_utf8(raw).unwrap();
            let length = head
                .lines()
                .find_map(|line| {
                    line.split_once(':')
                        .filter(|(name, _)| name.eq_ignore_ascii_case("content-length"))
                        .map(|(_, value)| value.trim().parse::<usize>().unwrap())
                })
                .unwrap();
            assert!(length <= 4096);
            let mut body = vec![0; length];
            socket.read_exact(&mut body).await.unwrap();
            socket.write_all(format!("HTTP/1.1 302 Found\r\nContent-Length: 0\r\nLocation: {location}\r\nConnection: close\r\n\r\n").as_bytes()).await.unwrap();
            assert!(split > 0);
            (head, body)
        });
        let effects = Effects {
            core: Arc::downgrade(&core),
        };
        let result = effects
            .deliver_webhook(ori_guard::WebhookRequest {
                url,
                body: b"what do ya want for nothing?".to_vec(),
                secret: "Jefe".into(),
                notification_id: "synthetic-notification".into(),
            })
            .await
            .unwrap();
        assert_eq!(result.delivered, Some(false));
        assert_eq!(result.status, Some(302));
        let (head, body) = tokio::time::timeout(Duration::from_secs(3), capture)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(body, b"what do ya want for nothing?");
        let headers = head.to_ascii_lowercase();
        assert!(headers.starts_with("post /notify http/1.1\r\n"));
        assert!(headers.contains("content-type: application/json\r\n"));
        assert!(
            headers.contains("x-openradioinspector-notification-id: synthetic-notification\r\n")
        );
        // RFC4231 case2: independent published expected HMAC, not a second
        // invocation of the production implementation.
        assert!(headers.contains("x-openradioinspector-signature: sha256=5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843\r\n"));
        assert!(
            tokio::time::timeout(Duration::from_millis(20), redirect.accept())
                .await
                .is_err()
        );
        core.close().await.unwrap();
    }
}

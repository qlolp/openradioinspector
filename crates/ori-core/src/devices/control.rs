//! The existing Worker owns every control exchange and the one serial actor.
//! Captured references identify that worker, never a replacement by target alone.
use super::*;
use ori_control::{
    AdapterCommand, AdapterReply, BoundExecution, CachedInstrument, CancellationOutcome,
    ControlError, ExecutedStep, ExecutionOutcome, ExecutionPriority, ExecutionRequest,
    InstrumentFamily, Operation, OperationKind, OwnerAuthority, OwnerReceipt, RevisionEffect,
};
use ori_instruments::{DeviceMode, OperationInterval, TinySaState};
use std::sync::atomic::AtomicU8;

type Result<T> = ori_control::Result<T>;
const QUEUED: u8 = 0;
const EXECUTING: u8 = 1;
const DISCARDED: u8 = 2;
const FINISHED: u8 = 3;
const MAX_RECEIPTS: usize = 192;

pub(super) struct State {
    connection: String,
    cache: Mutex<Cache>,
    executing: AtomicBool,
    paused: AtomicBool,
    pause_reason: Mutex<Option<&'static str>>,
}
struct Cache {
    revision: u64,
    configuration_changed_at: std::time::Instant,
    published_revision: u64,
    published_state: Option<TinySaState>,
    pending: BTreeMap<String, Option<(TinySaState, u64)>>,
}
impl State {
    pub(super) fn new(connection: String, native: Option<&Native>, paused: bool) -> Self {
        Self {
            connection,
            cache: Mutex::new(Cache {
                revision: 0,
                configuration_changed_at: std::time::Instant::now(),
                published_revision: 0,
                published_state: match native {
                    Some(Native::Analyzer(actor)) => Some(actor.state()),
                    _ => None,
                },
                pending: BTreeMap::new(),
            }),
            executing: AtomicBool::new(false),
            paused: AtomicBool::new(paused),
            pause_reason: Mutex::new(paused.then_some("guard_manual_ownership")),
        }
    }
    pub(super) fn revision(&self) -> u64 {
        self.cache.lock().expect("control cache poisoned").revision
    }
    pub(super) fn configuration(&self) -> (u64, std::time::Instant) {
        let cache = self.cache.lock().expect("control cache poisoned");
        (cache.revision, cache.configuration_changed_at)
    }
    pub(super) fn refresh(&self, native: Option<&Native>) {
        if let Some(Native::Analyzer(actor)) = native {
            let mut cache = self.cache.lock().expect("control cache poisoned");
            if cache.pending.is_empty() {
                cache.published_state = Some(actor.state());
            }
        }
    }
    fn reserve(&self, id: &str, priority: ExecutionPriority) -> Result<()> {
        let mut cache = self.cache.lock().map_err(|_| ControlError::Closed)?;
        let maximum = if priority == ExecutionPriority::OutputOff {
            8
        } else {
            7
        };
        if cache.pending.len() >= maximum || cache.pending.contains_key(id) {
            return Err(ControlError::Conflict(
                "bounded control publication slots unavailable",
            ));
        }
        cache.pending.insert(id.to_owned(), None);
        Ok(())
    }
    fn acknowledge(&self, id: String, state: TinySaState, revision: u64) -> Result<()> {
        let mut cache = self.cache.lock().map_err(|_| ControlError::Closed)?;
        let pending = cache.pending.get_mut(&id).ok_or(ControlError::Persistence(
            "original publication reservation missing".into(),
        ))?;
        *pending = Some((state, revision));
        if cache.revision != revision {
            cache.revision = revision;
            cache.configuration_changed_at = std::time::Instant::now();
        }
        Ok(())
    }
    fn publish(&self, id: &str) {
        let mut cache = self.cache.lock().expect("control cache poisoned");
        if let Some(Some((state, revision))) = cache.pending.remove(id)
            && revision >= cache.published_revision
        {
            cache.published_state = Some(state);
            cache.published_revision = revision;
        }
    }
    pub(super) fn paused(&self) -> bool {
        self.paused.load(Ordering::Acquire)
    }
    fn status(&self, native: Option<&Native>) -> Value {
        let cache = self.cache.lock().expect("control cache poisoned");
        let mut state = cache.published_state.clone();
        if let Some(Native::Analyzer(actor)) = native {
            let current = actor.state();
            if current.restart_required || current.closed {
                state = Some(current);
            } else if (self.executing.load(Ordering::Acquire)
                || cache.revision > cache.published_revision)
                && let Some(state) = state.as_mut()
            {
                state.device_mode = DeviceMode::Unknown;
                state.settings_source = "unknown".into();
            }
        }
        let mut status = state
            .as_ref()
            .map(|state| {
                ori_control::status_from_tinysa(state, &self.connection, cache.published_revision)
            })
            .unwrap_or_else(disconnected_status);
        status["acquisition_paused"] = json!(self.paused());
        status["acquisition_pause_reason"] = json!(
            *self
                .pause_reason
                .lock()
                .expect("control pause reason poisoned")
        );
        status
    }
    pub(super) fn project(&self, native: Option<&Native>, current: &mut Value) {
        if matches!(native, Some(Native::Analyzer(_))) {
            let status = self.status(native);
            current["instrument"] = status.clone();
            current["device_mode"] = status["device_mode"].clone();
            current["settings_source"] = status["settings_source"].clone();
            if status["settings_source"] != "command_acknowledged" {
                current["generator_output"] = json!("unknown");
                current["generator_output_source"] = json!("unknown");
            }
            if status["settings_source"] == "command_acknowledged"
                && let Some(enabled) = status["settings"]["output_enabled"].as_bool()
            {
                current["generator_output"] = json!(if enabled { "on" } else { "off" });
                current["generator_output_source"] =
                    json!("command_acknowledged_no_physical_readback");
            }
            current["acquisition_paused"] = json!(self.paused());
            current["acquisition_pause_reason"] = status["acquisition_pause_reason"].clone();
            if self.paused() {
                current["source_coverage"] = json!("paused");
            }
        }
    }
}
fn disconnected_status() -> Value {
    json!({"mode":"monitoring","revision":0,"profile_verified":false,"lease_active":false,"lease_seconds":0,"settings":{},"settings_source":"unknown","settings_readback":false,"device_mode":"unknown","device_mode_readback":false,"generator_output_readback":false,"hardware_validation":"not_run"})
}

type CleanupBarrier = Arc<dyn Fn() -> DeviceFuture<'static, ()> + Send + Sync>;
#[derive(Default)]
pub(super) struct Receipts {
    entries: Mutex<BTreeMap<String, Weak<Worker>>>,
    barrier: Mutex<Option<CleanupBarrier>>,
}
impl Receipts {
    fn retain(&self, worker: &Arc<Worker>) -> Result<()> {
        let mut entries = self.entries.lock().map_err(|_| ControlError::Closed)?;
        entries.retain(|_, weak| {
            weak.upgrade()
                .is_some_and(|value| !value.finished_ok() || Arc::strong_count(&value) > 2)
        });
        if !entries.contains_key(&worker.instance) && entries.len() >= MAX_RECEIPTS {
            return Err(ControlError::Conflict("owned instrument receipt capacity"));
        }
        entries.insert(worker.instance.clone(), Arc::downgrade(worker));
        Ok(())
    }
    fn original(&self, owner: &OwnerReceipt) -> Result<Arc<Worker>> {
        let worker = self
            .entries
            .lock()
            .map_err(|_| ControlError::Closed)?
            .get(owner.worker_instance())
            .and_then(Weak::upgrade)
            .ok_or(ControlError::Unavailable(
                "original_owned_worker_unavailable",
            ))?;
        if worker.stream.session_id != owner.target().session_id
            || worker.stream.id != owner.target().stream_id
            || worker.control.connection != owner.connection_id()
        {
            return Err(ControlError::Denied);
        }
        Ok(worker)
    }
}
struct Authority {
    target: ori_control::Target,
    worker: Arc<Worker>,
    gate: ExecutionGate,
}
impl OwnerAuthority for Authority {
    fn target(&self) -> &ori_control::Target {
        &self.target
    }
    fn worker_instance(&self) -> &str {
        &self.worker.instance
    }
    fn connection_id(&self) -> &str {
        &self.worker.control.connection
    }
    fn revalidate(&self) -> Result<()> {
        self.worker
            .live()
            .and_then(|()| self.gate.check(Some(&self.worker)))
            .map_err(|_| ControlError::Denied)
    }
}
#[derive(Clone)]
pub(crate) struct CapturedDevice {
    worker: Arc<Worker>,
    owner: OwnerReceipt,
}
impl CapturedDevice {
    pub(crate) fn worker_instance(&self) -> &str {
        &self.worker.instance
    }
    pub(crate) fn connection_id(&self) -> &str {
        &self.worker.control.connection
    }
    pub(crate) fn is_alive(&self) -> bool {
        self.worker.live().is_ok() && self.worker.snapshot().alive
    }
    pub(crate) fn owner(&self) -> OwnerReceipt {
        self.owner.clone()
    }
    pub(crate) fn matches(&self, instance: &str, connection: &str) -> bool {
        self.worker.instance == instance && self.worker.control.connection == connection
    }
    pub(crate) fn revalidate(&self) -> Result<()> {
        self.owner.revalidate()
    }
    pub(crate) fn revision(&self) -> u64 {
        self.worker.control.revision()
    }
    pub(crate) fn validate_audio(&self, revision: u64, acknowledged: &TinySaState) -> Result<()> {
        self.revalidate()?;
        let Some(Native::Analyzer(actor)) = &self.worker.native else {
            return Err(ControlError::Denied);
        };
        let current = actor.state();
        if self.revision() != revision
            || current.restart_required
            || current.closed
            || current.device_mode != acknowledged.device_mode
            || current.control_mode != acknowledged.control_mode
            || current.settings_source != acknowledged.settings_source
            || current.settings != acknowledged.settings
        {
            return Err(ControlError::Denied);
        }
        Ok(())
    }
}
pub(super) struct Request {
    request: ExecutionRequest,
    response: oneshot::Sender<Result<ExecutionOutcome>>,
    stage: Arc<AtomicU8>,
}

impl DeviceManager {
    pub(crate) fn instrument_instance(&self, target: &Target) -> DeviceResult<Option<String>> {
        Ok(self
            .registry
            .lock()
            .map_err(|_| DeviceError::Closed)?
            .workers
            .get(target)
            .map(|worker| worker.instance.clone()))
    }
    pub(crate) fn install_instrument_cleanup_barrier(
        &self,
        barrier: CleanupBarrier,
    ) -> DeviceResult<()> {
        let mut slot = self
            .control_receipts
            .barrier
            .lock()
            .map_err(|_| DeviceError::Closed)?;
        if slot.is_some() {
            return Err(DeviceError::Invalid(
                "Instrument cleanup barrier already installed".into(),
            ));
        }
        *slot = Some(barrier);
        Ok(())
    }
    /// Called only after the canonical terminal event/artifact transaction ACK.
    pub(crate) fn publish_instrument(&self, id: &str) {
        let entries = self
            .control_receipts
            .entries
            .lock()
            .expect("control receipt registry poisoned");
        for worker in entries.values().filter_map(Weak::upgrade) {
            worker.control.publish(id);
        }
    }
    /// Called only after HTTP has captured its original AuthReceipt/Guard grant.
    /// The actual worker is copied once before any queue/actor await.
    pub(crate) fn capture_instrument(
        self: &Arc<Self>,
        target: &Target,
        gate: ExecutionGate,
    ) -> Result<CapturedDevice> {
        let retained: RetainedWorkerHandle =
            self.capture_worker(target, gate)
                .map_err(|error| match error {
                    DeviceError::Closed => ControlError::Closed,
                    DeviceError::Worker(_) => {
                        ControlError::Unavailable("owned_instrument_not_connected")
                    }
                    _ => ControlError::Denied,
                })?;
        let worker = retained.original_worker();
        if retained.worker_instance() != worker.instance
            || retained.connection_id() != worker.control.connection
        {
            return Err(ControlError::Denied);
        }
        if !matches!(worker.native, Some(Native::Analyzer(_))) {
            return Err(ControlError::Unavailable(
                "verified_tinysa_connection_required",
            ));
        }
        let gate = retained.execution_gate();
        let target = retained.target();
        self.control_receipts.retain(&worker)?;
        let owner = OwnerReceipt::from_backend(Arc::new(Authority {
            target: ori_control::Target {
                session_id: target.session_id.clone(),
                stream_id: target.stream_id.clone(),
            },
            worker: worker.clone(),
            gate,
        }))?;
        Ok(CapturedDevice { worker, owner })
    }
    /// Read-only cache projection. This creates no execution permission.
    pub(crate) fn instrument_snapshot(
        &self,
        target: &ori_control::Target,
        owner: Option<OwnerReceipt>,
        science: bool,
        audio: bool,
    ) -> Result<CachedInstrument> {
        let worker = self
            .registry
            .lock()
            .map_err(|_| ControlError::Closed)?
            .workers
            .get(&Target::new(&target.session_id, &target.stream_id))
            .cloned();
        let status = worker
            .as_ref()
            .map(|worker| worker.control.status(worker.native.as_ref()))
            .unwrap_or_else(disconnected_status);
        let family = if worker
            .as_ref()
            .is_some_and(|w| matches!(w.native, Some(Native::Receiver(_))))
        {
            InstrumentFamily::ArDv1
        } else {
            InstrumentFamily::TinySa
        };
        let owner = owner.filter(|owner| {
            worker.as_ref().is_some_and(|worker| {
                worker.instance == owner.worker_instance()
                    && worker.control.connection == owner.connection_id()
                    && worker.live().is_ok()
            })
        });
        Ok(CachedInstrument {
            target: target.clone(),
            family,
            control_revision: status["revision"].as_u64().unwrap_or(0),
            status,
            owner,
            supported_operations: OperationKind::ALL.iter().copied().collect(),
            guard_claimed: false,
            coverage_bridge_available: false,
            science_available: science,
            audio_available: audio,
        })
    }
    pub(crate) async fn instrument(
        self: &Arc<Self>,
        request: ExecutionRequest,
    ) -> Result<ExecutionOutcome> {
        request.owner.revalidate()?;
        let worker = self.control_receipts.original(&request.owner)?;
        let priority = request.operation.priority();
        let check = request.check.clone();
        let stage = Arc::new(AtomicU8::new(QUEUED));
        let (response, mut received) = oneshot::channel();
        let command = Request {
            request,
            response,
            stage: stage.clone(),
        };
        if priority == ExecutionPriority::OutputOff {
            worker.priority.try_send(command).map_err(|_| {
                ControlError::Conflict("reserved output-off executor lane unavailable")
            })?;
        } else {
            worker
                .commands
                .try_send(Command::Instrument(Box::new(command)))
                .map_err(|_| ControlError::Conflict("owned executor command queue full"))?;
        }
        loop {
            tokio::select! {
                response=&mut received=>return response.map_err(|_|ControlError::Indeterminate("original worker stopped before completion".into()))?,
                _=tokio::time::sleep(Duration::from_millis(25))=>{
                    if check().is_err(){match stage.compare_exchange(QUEUED,DISCARDED,Ordering::AcqRel,Ordering::Acquire){Ok(_)=>return Err(ControlError::Denied),Err(EXECUTING)=>worker.request_stop(),_=>{}}}
                }
            }
        }
    }
    pub(crate) async fn cancel_instrument(
        self: &Arc<Self>,
        owner: &OwnerReceipt,
    ) -> Result<CancellationOutcome> {
        let worker = self.control_receipts.original(owner)?;
        // Cleanup is intentionally not authorized by a fresh grant/current actor.
        worker.request_stop();
        tokio::time::timeout(self.options.stop_timeout, worker.completion.wait())
            .await
            .map_err(|_| {
                ControlError::Indeterminate("original worker join deadline exceeded".into())
            })?
            .map_err(|_| ControlError::Persistence("original worker cleanup failed".into()))?;
        if let Some(released) = &worker.released {
            tokio::time::timeout(self.options.stop_timeout, released.wait())
                .await
                .map_err(|_| {
                    ControlError::Indeterminate("original transport drop not confirmed".into())
                })?;
        } else {
            return Err(ControlError::Unavailable("original_native_transport_proof"));
        }
        Ok(CancellationOutcome {
            original_worker_joined: true,
            original_transport_dropped: true,
            tainted: worker
                .native
                .as_ref()
                .is_some_and(|native| native.current()["restart_required"] == true),
        })
    }
    /// Invalidate existing/pending control setup before waiting for its bind lock.
    /// This is trusted stop intent, never a new operational permission.
    pub(crate) fn request_instrument_stop(&self, target: &Target) -> DeviceResult<()> {
        let mut registry = self.registry.lock().map_err(|_| DeviceError::Closed)?;
        registry
            .lifecycles
            .entry(target.clone())
            .or_default()
            .generation
            .fetch_add(1, Ordering::AcqRel);
        if let Some(worker) = registry.workers.get(target) {
            worker.request_stop();
        }
        Ok(())
    }
    fn cancel_start_identity(
        &self,
        target: &Target,
        identity: &str,
    ) -> DeviceResult<Option<Arc<Worker>>> {
        let registry = self.registry.lock().map_err(|_| DeviceError::Closed)?;
        if registry
            .pending
            .get(target)
            .is_some_and(|id| id == identity)
            && let Some(lifecycle) = registry.lifecycles.get(target)
        {
            lifecycle.generation.fetch_add(1, Ordering::AcqRel);
        }
        let original = registry
            .workers
            .get(target)
            .filter(|worker| worker.instance == identity)
            .cloned();
        if let Some(worker) = &original {
            worker.request_stop();
        }
        Ok(original)
    }
    /// Explicit connect under the ORIGINAL admitted grant. The nonce predates
    /// every await and cancellation never selects a replacement by target.
    pub(crate) async fn start_instrument(
        self: &Arc<Self>,
        target: &Target,
        gate: ExecutionGate,
        paused: bool,
    ) -> DeviceResult<(WorkerStatus, super::RetainedWorkerHandle)> {
        Self::stream(&self.session(&target.session_id).await?, &target.stream_id)?;
        gate.check(None)?;
        let ticket = self.admit(target.clone())?;
        let identity = ticket.pending.identity.clone();
        let manager = self.clone();
        let original_gate = gate.clone();
        let mut task =
            tokio::spawn(async move { manager.start_admitted(ticket, gate, paused).await });
        let mut cancelled = false;
        let mut original = None;
        loop {
            tokio::select! {
                result=&mut task=>{
                    let result=match result {
                        Ok(result)=>result,
                        Err(_)=>{
                            if let Some(worker)=self.cancel_start_identity(target,&identity)?{worker.stop_and_join(self.options.stop_timeout).await?;}
                            return Err(DeviceError::Worker("Owned instrument start failed".into()));
                        }
                    };
                    if cancelled||original_gate.authorize().is_err(){
                        let worker=original.or(self.cancel_start_identity(target,&identity)?);
                        if let Some(worker)=worker{worker.stop_and_join(self.options.stop_timeout).await?;}
                        return Err(DeviceError::Denied);
                    }
                    let status = result?;
                    let retained = self.retain_started_worker(target, &identity, original_gate)?;
                    return Ok((status, retained));
                },
                _=tokio::time::sleep(Duration::from_millis(25))=>{
                    if original_gate.authorize().is_err(){
                        cancelled=true;
                        if let Some(worker)=self.cancel_start_identity(target,&identity)?{original=Some(worker);}
                    }
                    // The enclosing owned setup registry applies its bounded
                    // close deadline. Retain this SAME inner JOIN when a
                    // canonical ready write is pending; never detach it merely
                    // to return an early cleanup error to the HTTP waiter.
                },
            }
        }
    }
}

impl Worker {
    pub(super) async fn pause_non_raw_monitoring(
        &self,
        sample: ori_instruments::SpectrumSample,
    ) -> DeviceResult<()> {
        let reason = "non_canonical_monitoring_requires_verified_receive_preparation";
        let message = "Автосбор исходного спектра приостановлен: получен processed/zero-span trace или неподдерживаемая единица. Требуется подтверждённая подготовка приёмного профиля; перезапуск сам по себе не подтверждает состояние trace.";
        let payload = ori_contracts::to_json_vec(&sample)
            .map_err(|_| DeviceError::Invalid("Non-finite monitoring artifact".into()))?;
        let at = crate::utc_now();
        let kind = match sample.kind {
            ori_instruments::SpectrumKind::RawSpectrum => ori_control::ArtifactKind::RawScan,
            ori_instruments::SpectrumKind::ZeroSpan => ori_control::ArtifactKind::ZeroSpan,
            ori_instruments::SpectrumKind::ProcessedTrace => {
                ori_control::ArtifactKind::ProcessedTrace
            }
        };
        let item=ori_control::ClosedArtifact::from_payload(ori_control::Target{session_id:self.stream.session_id.clone(),stream_id:self.stream.id.clone()},kind,"application/json","tinysa-monitoring-noncanonical.json",payload,json!({"occurred_at":at,"started_at":sample.interval.started_at.to_rfc3339_opts(chrono::SecondsFormat::Micros,true),"ended_at":sample.interval.ended_at.to_rfc3339_opts(chrono::SecondsFormat::Micros,true),"source":"automatic_monitoring_noncanonical","connection_id":self.control.connection,"revision":self.control.revision(),"hardware_validation":"not_run","source_coverage":"paused","reason":reason}),ori_control::MAX_BLOB_BYTES).map_err(|_|DeviceError::Invalid("Invalid complete monitoring artifact".into()))?;
        let summary = item.summary();
        let artifact = ori_archive::Artifact {
            id: item.id().into(),
            session_id: self.stream.session_id.clone(),
            stream_id: self.stream.id.clone(),
            schema_version: 1,
            kind: summary["kind"].as_str().expect("closed kind").into(),
            mime_type: item.mime_type().into(),
            filename: item.filename().into(),
            sha256: item.sha256().into(),
            created_at: summary["created_at"].as_str().expect("closed UTC").into(),
            metadata: item.metadata().clone(),
            payload: item.into_payload(),
        };
        self.archive.apply(vec![Mutation::Artifact(artifact),Mutation::Event(EventDraft::now("device.acquisition.paused",Some(self.stream.session_id.clone()),json!({"stream_id":self.stream.id,"state":"degraded","source_coverage":"paused","reason":reason,"error":message,"artifact":summary}))) ]).await?;
        // One canonical ACK precedes both the visible warning and poll pause.
        *self
            .control
            .pause_reason
            .lock()
            .map_err(|_| DeviceError::Closed)? = Some(reason);
        self.control.paused.store(true, Ordering::Release);
        let mut status = self.status.lock().map_err(|_| DeviceError::Closed)?;
        status.state = "degraded".into();
        status.last_error = Some(message.into());
        Ok(())
    }
    pub(super) async fn instrument_cleanup_barrier(&self) -> DeviceResult<()> {
        let barrier = self
            .manager
            .upgrade()
            .and_then(|manager| manager.control_receipts.barrier.lock().ok()?.clone());
        if let Some(barrier) = barrier {
            barrier().await
        } else {
            Ok(())
        }
    }
    pub(super) async fn apply_command(self: &Arc<Self>, adapter: &mut Adapter, command: Command) {
        match command {
            Command::Tune {
                frequency,
                mode,
                gate,
                stage,
                response,
            } => {
                self.apply_tune_command(adapter, frequency, mode, gate, stage, response)
                    .await;
            }
            Command::Instrument(request) => self.apply_instrument(*request).await,
        }
    }
    pub(super) async fn apply_priority(self: &Arc<Self>, request: Request) {
        self.apply_instrument(request).await;
    }
    pub(super) fn reject_command(command: Command) {
        match command {
            Command::Tune {
                response, stage, ..
            } => {
                stage.finish();
                let _ = response.send(Err(DeviceError::Cancelled));
            }
            Command::Instrument(request) => {
                let _ = request.response.send(Err(ControlError::Closed));
            }
        }
    }
    pub(super) fn reject_priority(request: Request) {
        let _ = request.response.send(Err(ControlError::Closed));
    }
    async fn apply_instrument(self: &Arc<Self>, command: Request) {
        let Request {
            request,
            response,
            stage,
        } = command;
        let result = async {
            if stage
                .compare_exchange(QUEUED, EXECUTING, Ordering::AcqRel, Ordering::Acquire)
                .is_err()
            {
                return Err(ControlError::Denied);
            }
            self.live().map_err(|_| ControlError::Denied)?;
            request.owner.revalidate()?;
            (request.check)().map_err(|_| ControlError::Denied)?;
            if request.owner.worker_instance() != self.instance
                || request.owner.connection_id() != self.control.connection
            {
                return Err(ControlError::Denied);
            }
            let Some(Native::Analyzer(actor)) = &self.native else {
                return Err(ControlError::Unavailable(
                    "verified_tinysa_connection_required",
                ));
            };
            let bound = BoundExecution::bind(
                request.owner.clone(),
                request.expected_revision,
                self.control.revision(),
            )?;
            let plan = ori_control::plan_operation(&request.operation, &actor.state())?;
            // Reserve bounded publication ownership BEFORE the first RF write.
            // The off lane retains its own last slot while normal results wait.
            self.control
                .reserve(&request.operation_id, request.operation.priority())?;
            let revision = if plan.revision_effect == RevisionEffect::AdvanceAfterAcknowledgement {
                bound
                    .control_revision()
                    .checked_add(1)
                    .ok_or(ControlError::Conflict("control revision exhausted"))?
            } else {
                bound.control_revision()
            };
            let original = request.check.clone();
            let worker = self.clone();
            let expected = bound.control_revision();
            let check: ori_instruments::ExecutionCheck = Arc::new(move || {
                worker
                    .live()
                    .map_err(|_| ori_instruments::InstrumentError::PolicyDenied)?;
                original()?;
                if worker.control.revision() != expected {
                    return Err(ori_instruments::InstrumentError::PolicyDenied);
                }
                Ok(())
            });
            let mut commands = plan.commands;
            // Separate query-only verification from actual screen-transfer time.
            if matches!(request.operation, Operation::Capture { .. })
                && !actor.state().control_profile_verified
            {
                commands.insert(0, AdapterCommand::TinySa(TinySaRequest::Inspect));
            }
            let began = now();
            let mut steps = Vec::with_capacity(commands.len());
            self.active_io.store(true, Ordering::Release);
            self.control.executing.store(
                plan.revision_effect == RevisionEffect::AdvanceAfterAcknowledgement,
                Ordering::Release,
            );
            let result = async {
                for command in commands {
                    let AdapterCommand::TinySa(command) = command else {
                        return Err(ControlError::Unavailable(
                            "receiver_operation_not_in_tinysa_registry",
                        ));
                    };
                    let started_at = now();
                    let pending = actor.enqueue_checked(command, check.clone()).map_err(|e| {
                        ori_control::classify_adapter_error(e, actor.state().restart_required)
                    })?;
                    let reply = pending.result().await.map_err(|e| {
                        ori_control::classify_adapter_error(e, actor.state().restart_required)
                    })?;
                    let interval = match &reply {
                        TinySaReply::RawSpectrum(sample) => sample.interval.clone(),
                        TinySaReply::Spectrum(sample) => sample.interval.clone(),
                        TinySaReply::Text(text) => text.interval.clone(),
                        _ => OperationInterval {
                            started_at,
                            ended_at: now(),
                        },
                    };
                    steps.push(ExecutedStep {
                        reply: AdapterReply::TinySa(reply),
                        interval,
                    });
                }
                (check)().map_err(|_| ControlError::Denied)?;
                Ok(())
            }
            .await;
            self.control.executing.store(false, Ordering::Release);
            self.active_io.store(false, Ordering::Release);
            result?;
            let state = actor.state();
            self.control
                .acknowledge(request.operation_id, state.clone(), revision)?;
            self.status
                .lock()
                .map_err(|_| ControlError::Closed)?
                .commands_completed += 1;
            let interval = steps
                .iter()
                .find_map(|step| match &step.reply {
                    AdapterReply::TinySa(TinySaReply::Screen(_)) => Some(step.interval.clone()),
                    _ => None,
                })
                .unwrap_or(OperationInterval {
                    started_at: began,
                    ended_at: now(),
                });
            let mut status =
                ori_control::status_from_tinysa(&state, &self.control.connection, revision);
            status["acquisition_paused"] = json!(self.control.paused());
            Ok(ExecutionOutcome {
                execution: bound,
                applied_revision: revision,
                status,
                steps,
                interval,
            })
        }
        .await;
        self.control.executing.store(false, Ordering::Release);
        self.active_io.store(false, Ordering::Release);
        stage.store(FINISHED, Ordering::Release);
        let _ = response.send(result);
    }
}
fn now() -> chrono::DateTime<chrono::Utc> {
    chrono::DateTime::from_timestamp_micros(chrono::Utc::now().timestamp_micros())
        .expect("current UTC microseconds")
}

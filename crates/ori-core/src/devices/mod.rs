//! Core-owned independent device loops. Public controls carry a cached policy
//! gate to the actual actor execution; trusted measurement reads use liveness.
pub(crate) mod control;
mod receipt;
pub(crate) use receipt::RetainedWorkerHandle;
mod acquisition;
mod simulator;
pub(crate) use acquisition::AcquisitionHealth;
#[cfg(test)]
mod tests;
mod transport;
use ori_archive::{Archive, EventDraft, Mutation};
use ori_contracts::{
    DeviceRole, DeviceStream, Session, from_json_slice, parse_json_value, to_json_vec,
};
use ori_instruments::{
    ArDv1, ArDv1Actor, ArDv1Reply, ArDv1Request, IoLimits, ReceiveConfig, ReceiverConfig,
    ReceiverMode, ReceiverProfile, TinySa, TinySaActor, TinySaReply, TinySaRequest,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    future::Future,
    pin::Pin,
    sync::{
        Arc, Mutex, Weak,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    time::Duration,
};
use tokio::sync::{Notify, mpsc, oneshot};
#[cfg(feature = "serial")]
pub use transport::NativeSerialFactory;
pub use transport::{BoxTransport, Transport, TransportFactory};

#[derive(Debug, thiserror::Error)]
pub enum DeviceError {
    #[error("{0}")]
    Invalid(String),
    #[error("{0}")]
    Worker(String),
    #[error("{0}")]
    Unsupported(String),
    #[error("Control ownership changed; command was not executed")]
    Denied,
    #[error("Device command queue is full")]
    QueueFull,
    #[error("Worker start was cancelled by stop")]
    Cancelled,
    #[error("Device manager is closed")]
    Closed,
    #[error("Automatic raw monitoring is paused pending verified receive preparation")]
    NonRawMonitoring(Box<ori_instruments::SpectrumSample>),
    #[error("Instrument operation failed: {0}")]
    Instrument(#[from] ori_instruments::InstrumentError),
    #[error("Archive operation failed: {0}")]
    Archive(#[from] ori_archive::ArchiveError),
    #[error("Device I/O failed: {0}")]
    Io(#[from] std::io::Error),
}
pub type DeviceResult<T> = std::result::Result<T, DeviceError>;
pub type DeviceFuture<'a, T> = Pin<Box<dyn Future<Output = DeviceResult<T>> + Send + 'a>>;

#[derive(Clone)]
pub struct DeviceOptions {
    /// Absent by default. A native factory requires an explicit operator flag.
    pub transport: Option<Arc<dyn TransportFactory>>,
    pub io_limits: IoLimits,
    pub queue_capacity: usize,
    pub max_workers: usize,
    pub connect_timeout: Duration,
    pub stop_timeout: Duration,
}
impl Default for DeviceOptions {
    fn default() -> Self {
        Self {
            transport: None,
            io_limits: IoLimits::default(),
            queue_capacity: 8,
            max_workers: 32,
            connect_timeout: Duration::from_secs(5),
            stop_timeout: Duration::from_secs(5),
        }
    }
}
impl DeviceOptions {
    pub(crate) fn validate(&self) -> DeviceResult<()> {
        if !(1..=64).contains(&self.queue_capacity)
            || !(1..=32).contains(&self.max_workers)
            || self.connect_timeout.is_zero()
            || self.connect_timeout > Duration::from_secs(10)
            || self.stop_timeout.is_zero()
            || self.stop_timeout > Duration::from_secs(10)
        {
            return Err(DeviceError::Invalid(
                "Invalid bounded device configuration".into(),
            ));
        }
        Ok(())
    }
}
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub(crate) struct Target {
    pub session_id: String,
    pub stream_id: String,
}
impl Target {
    pub fn new(session: &str, stream: &str) -> Self {
        Self {
            session_id: session.into(),
            stream_id: stream.into(),
        }
    }
}
type Policy = Arc<dyn Fn(&Target) -> DeviceResult<()> + Send + Sync>;
/// Opaque execution authorization; never Debug or serialized. Guard callbacks
/// capture the original permit, never mint a fresh epoch after a queue wait.
#[derive(Clone)]
pub(crate) struct ExecutionGate {
    check: Arc<dyn Fn() -> DeviceResult<()> + Send + Sync>,
    expected_worker: Option<String>,
}
impl ExecutionGate {
    pub(crate) fn authorize(&self) -> DeviceResult<()> {
        (self.check)()
    }
    pub fn new(
        check: impl Fn() -> DeviceResult<()> + Send + Sync + 'static,
        expected_worker: Option<String>,
    ) -> Self {
        Self {
            check: Arc::new(check),
            expected_worker,
        }
    }
    fn check(&self, worker: Option<&Worker>) -> DeviceResult<()> {
        (self.check)()?;
        if self
            .expected_worker
            .as_deref()
            .is_some_and(|expected| worker.is_none_or(|worker| worker.instance != expected))
        {
            return Err(DeviceError::Denied);
        }
        Ok(())
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct WorkerStatus {
    pub session_id: String,
    pub stream_id: String,
    pub device: String,
    pub adapter: Option<String>,
    pub state: String,
    pub alive: bool,
    pub measurements: u64,
    pub commands_completed: u64,
    pub consecutive_errors: u64,
    pub last_error: Option<String>,
    pub last_measurement_id: Option<String>,
    pub last_measurement_at: Option<String>,
    pub started_at: Option<String>,
    pub ended_at: Option<String>,
    pub current: Value,
    #[serde(skip)]
    pub worker_instance: Option<String>,
}
/// Host-observed successful safe-prepare receipt, not an RF measurement. A
/// report is exposed only while its original owned connection remains known.
#[derive(Clone, Debug, Serialize)]
pub struct PrepareReport {
    pub profile: String,
    pub acknowledged_at: String,
    pub input_mode_acknowledged: bool,
    pub generator_off_acknowledged: bool,
    pub device_mode_readback: bool,
    pub generator_output_readback: bool,
    pub hardware_validation: String,
}
impl WorkerStatus {
    fn idle(stream: &DeviceStream) -> Self {
        Self {
            session_id: stream.session_id.clone(),
            stream_id: stream.id.clone(),
            device: stream.device.clone(),
            adapter: stream
                .settings
                .as_ref()
                .and_then(|s| s.get("adapter"))
                .and_then(Value::as_str)
                .map(str::to_owned),
            state: stream
                .state
                .clone()
                .unwrap_or_else(|| "disconnected".into()),
            alive: false,
            measurements: 0,
            commands_completed: 0,
            consecutive_errors: 0,
            last_error: None,
            last_measurement_id: None,
            last_measurement_at: None,
            started_at: None,
            ended_at: None,
            current: json!({}),
            worker_instance: None,
        }
    }
    pub(crate) fn value(&self) -> DeviceResult<Value> {
        value(self)
    }
}
fn value<T: Serialize>(input: &T) -> DeviceResult<Value> {
    let bytes =
        to_json_vec(input).map_err(|_| DeviceError::Worker("Device JSON is not finite".into()))?;
    parse_json_value(&bytes, 16 * 1024 * 1024)
        .map_err(|_| DeviceError::Worker("Device JSON exceeds budget".into()))
}
fn parse_session(value: &Value) -> DeviceResult<Session> {
    let bytes =
        to_json_vec(value).map_err(|_| DeviceError::Worker("Invalid archived session".into()))?;
    from_json_slice(&bytes, 16 * 1024 * 1024)
        .map_err(|_| DeviceError::Worker("Invalid archived session".into()))
}

#[derive(Clone)]
enum Native {
    Analyzer(TinySaActor),
    Receiver(ArDv1Actor),
}
impl Native {
    fn cancel(&self) {
        match self {
            Self::Analyzer(actor) => actor.cancel(),
            Self::Receiver(actor) => actor.cancel(),
        }
    }
    fn current(&self) -> Value {
        match self {
            Self::Analyzer(actor) => {
                let state = actor.state();
                json!({"instrument":state,"settings_source":state.settings_source,"settings_readback":false,"generator_output":"unknown","generator_output_readback":false,"restart_required":state.restart_required,"simulated":false,"hardware_validation":"not_run"})
            }
            Self::Receiver(actor) => {
                let state = actor.state();
                json!({"frequency_hz":state.frequency_hz,"mode":state.mode,"requested_frequency_hz":state.requested_frequency_hz,"requested_mode":state.requested_mode,"frequency_source":state.frequency_source,"mode_source":state.frequency_source,"frequency_readback":false,"receiver_firmware":state.firmware,"restart_required":state.restart_required,"simulated":false,"hardware_validation":"not_run"})
            }
        }
    }
    async fn close(&self) -> DeviceResult<()> {
        match self {
            Self::Analyzer(actor) => match actor.enqueue(TinySaRequest::Close)?.result().await? {
                TinySaReply::Closed => Ok(()),
                _ => Err(DeviceError::Worker(
                    "Unexpected analyzer close reply".into(),
                )),
            },
            Self::Receiver(actor) => match actor.enqueue(ArDv1Request::Close)?.result().await? {
                ArDv1Reply::Closed => Ok(()),
                _ => Err(DeviceError::Worker(
                    "Unexpected receiver close reply".into(),
                )),
            },
        }
    }
}
enum Adapter {
    Simulated(Box<simulator::Simulated>),
    Native(Native),
}
enum Command {
    Instrument(Box<control::Request>),
    Tune {
        frequency: u64,
        mode: Option<String>,
        gate: ExecutionGate,
        stage: Arc<receipt::TuneStage>,
        response: oneshot::Sender<DeviceResult<TuneOutcome>>,
    },
}
struct Lifecycle {
    lock: tokio::sync::Mutex<()>,
    generation: AtomicU64,
}
impl Default for Lifecycle {
    fn default() -> Self {
        Self {
            lock: tokio::sync::Mutex::new(()),
            generation: AtomicU64::new(0),
        }
    }
}
struct Ticket {
    target: Target,
    lifecycle: Arc<Lifecycle>,
    generation: u64,
    pending: PendingStart,
}
struct Registry {
    workers: BTreeMap<Target, Arc<Worker>>,
    lifecycles: BTreeMap<Target, Arc<Lifecycle>>,
    ports: BTreeMap<String, Target>,
    pending: BTreeMap<Target, String>,
    stopping: BTreeMap<String, usize>,
    closed: bool,
}
pub(crate) struct DeviceManager {
    archive: Archive,
    options: DeviceOptions,
    registry: Mutex<Registry>,
    policy: Mutex<Option<Policy>>,
    control_receipts: control::Receipts,
}
impl DeviceManager {
    pub fn validate_profiles(&self, session: &Session) -> DeviceResult<()> {
        for stream in &session.streams {
            let settings = stream.settings.clone().unwrap_or_default();
            simulator::period(&settings)?;
            simulator::integer(&settings, "max_consecutive_errors", 3, 1, 100)?;
            let adapter = settings
                .get("adapter")
                .and_then(Value::as_str)
                .unwrap_or("");
            if matches!(adapter, "tinysa-sim" | "ardv1-sim") {
                let _ = simulator::Simulated::new(stream)?;
            } else if matches!(adapter, "tinysa-serial" | "ardv1-serial") {
                if self.options.transport.is_none() {
                    return Err(DeviceError::Unsupported(
                        "Native serial connector is not enabled".into(),
                    ));
                }
                let _ = Profile::read(stream)?;
                let baud = simulator::integer(
                    &settings,
                    "baud",
                    if adapter == "tinysa-serial" {
                        9600
                    } else {
                        115200
                    },
                    9600,
                    115200,
                )?;
                if ![9600, 19200, 38400, 57600, 115200].contains(&baud) {
                    return Err(DeviceError::Invalid(
                        "Unsupported native serial baud".into(),
                    ));
                }
            } else {
                return Err(DeviceError::Unsupported(
                    "Explicit supported adapter required".into(),
                ));
            }
        }
        Ok(())
    }
    pub fn preparation(&self, target: &Target) -> DeviceResult<Option<PrepareReport>> {
        let worker = self
            .registry
            .lock()
            .map_err(|_| DeviceError::Closed)?
            .workers
            .get(target)
            .cloned();
        Ok(worker.and_then(|worker| worker.preparation()))
    }
    /// Original instance/revision comparison for Guard's cache-only receipt
    /// check. Authorization is separately checked with its original permit.
    pub fn validate_tune(
        &self,
        target: &Target,
        instance: &str,
        revision: u64,
        tuned: &Value,
    ) -> DeviceResult<()> {
        let worker = self
            .registry
            .lock()
            .map_err(|_| DeviceError::Closed)?
            .workers
            .get(target)
            .cloned()
            .ok_or(DeviceError::Denied)?;
        if worker.instance != instance {
            return Err(DeviceError::Denied);
        }
        worker.validate_tune(revision, tuned)
    }
    pub fn request_shutdown(&self) -> DeviceResult<()> {
        let mut registry = self.registry.lock().map_err(|_| DeviceError::Closed)?;
        registry.closed = true;
        for lifecycle in registry.lifecycles.values() {
            lifecycle.generation.fetch_add(1, Ordering::AcqRel);
        }
        for worker in registry.workers.values() {
            worker.request_stop();
        }
        Ok(())
    }
    pub async fn authorize_public_session(self: &Arc<Self>, session: &str) -> DeviceResult<()> {
        for stream in self.session(session).await?.streams {
            self.public_gate(Target::new(session, &stream.id))
                .authorize()?;
        }
        Ok(())
    }
    pub fn new(archive: Archive, options: DeviceOptions) -> Arc<Self> {
        Arc::new(Self {
            archive,
            options,
            registry: Mutex::new(Registry {
                workers: BTreeMap::new(),
                lifecycles: BTreeMap::new(),
                ports: BTreeMap::new(),
                pending: BTreeMap::new(),
                stopping: BTreeMap::new(),
                closed: false,
            }),
            policy: Mutex::new(None),
            control_receipts: control::Receipts::default(),
        })
    }
    pub fn attach_public_policy(&self, policy: Policy) -> DeviceResult<()> {
        let mut current = self.policy.lock().map_err(|_| DeviceError::Closed)?;
        if current.is_some() {
            return Err(DeviceError::Worker(
                "Device control policy already attached".into(),
            ));
        }
        *current = Some(policy);
        Ok(())
    }
    pub fn public_gate(self: &Arc<Self>, target: Target) -> ExecutionGate {
        let manager = Arc::downgrade(self);
        ExecutionGate::new(
            move || {
                let manager = manager.upgrade().ok_or(DeviceError::Closed)?;
                let policy = manager
                    .policy
                    .lock()
                    .map_err(|_| DeviceError::Closed)?
                    .clone();
                if let Some(policy) = policy {
                    policy(&target)?;
                }
                Ok(())
            },
            None,
        )
    }
    async fn session(&self, id: &str) -> DeviceResult<Session> {
        let value = self
            .archive
            .get_session(id)
            .await?
            .ok_or_else(|| DeviceError::Worker(format!("Сеанс не найден: {id}")))?;
        parse_session(&value)
    }
    fn stream<'a>(session: &'a Session, id: &str) -> DeviceResult<&'a DeviceStream> {
        session
            .streams
            .iter()
            .find(|stream| stream.id == id)
            .ok_or_else(|| DeviceError::Worker(format!("Поток не принадлежит сеансу: {id}")))
    }
    fn admit(self: &Arc<Self>, target: Target) -> DeviceResult<Ticket> {
        let mut registry = self.registry.lock().map_err(|_| DeviceError::Closed)?;
        if registry.closed {
            return Err(DeviceError::Closed);
        }
        if registry.stopping.contains_key(&target.session_id) {
            return Err(DeviceError::Cancelled);
        }
        // Cached/lifecycle entries are bounded too. Remove only fully drained,
        // unreferenced old targets; never evict a live/pending owner.
        if registry.lifecycles.len() >= 128 && !registry.lifecycles.contains_key(&target) {
            let removable = registry.lifecycles.iter().find_map(|(key, lifecycle)| {
                (!registry.pending.contains_key(key)
                    && registry
                        .workers
                        .get(key)
                        .is_none_or(|worker| worker.finished_ok())
                    && Arc::strong_count(lifecycle) == 1)
                    .then_some(key.clone())
            });
            if let Some(key) = removable {
                registry.lifecycles.remove(&key);
                registry.workers.remove(&key);
            } else {
                return Err(DeviceError::QueueFull);
            }
        }
        if registry.pending.len() >= self.options.max_workers
            || registry.pending.contains_key(&target)
        {
            return Err(DeviceError::QueueFull);
        }
        let lifecycle = registry
            .lifecycles
            .entry(target.clone())
            .or_default()
            .clone();
        let generation = lifecycle.generation.load(Ordering::Acquire);
        let identity = crate::new_id();
        registry.pending.insert(target.clone(), identity.clone());
        let pending = PendingStart {
            manager: self.clone(),
            target: target.clone(),
            identity,
            port: None,
        };
        Ok(Ticket {
            target,
            lifecycle,
            generation,
            pending,
        })
    }
    fn check_ticket(&self, ticket: &Ticket) -> DeviceResult<()> {
        let registry = self.registry.lock().map_err(|_| DeviceError::Closed)?;
        if registry.closed {
            return Err(DeviceError::Closed);
        }
        if registry.stopping.contains_key(&ticket.target.session_id)
            || registry.pending.get(&ticket.target) != Some(&ticket.pending.identity)
            || ticket.lifecycle.generation.load(Ordering::Acquire) != ticket.generation
        {
            return Err(DeviceError::Cancelled);
        }
        Ok(())
    }
    pub async fn start_stream(
        self: &Arc<Self>,
        session: &str,
        stream: &str,
        gate: ExecutionGate,
    ) -> DeviceResult<WorkerStatus> {
        Self::stream(&self.session(session).await?, stream)?;
        gate.check(None)?;
        let ticket = self.admit(Target::new(session, stream))?;
        let owned = self.clone();
        tokio::spawn(async move { owned.start_admitted(ticket, gate, false).await })
            .await
            .map_err(|_| DeviceError::Worker("Worker start task failed".into()))?
    }
    pub async fn start_session(self: &Arc<Self>, session: &str) -> DeviceResult<Vec<WorkerStatus>> {
        let manager = self.clone();
        self.start_session_gated(
            session,
            Arc::new(move |target| manager.public_gate(target.clone())),
        )
        .await
    }
    pub async fn start_session_gated(
        self: &Arc<Self>,
        session: &str,
        gates: Arc<dyn Fn(&Target) -> ExecutionGate + Send + Sync>,
    ) -> DeviceResult<Vec<WorkerStatus>> {
        let session = self.session(session).await?;
        if session.state != "running" {
            return Err(DeviceError::Worker(
                "Сначала запустите сеанс мониторинга".into(),
            ));
        }
        let mut admissions = Vec::new();
        for stream in &session.streams {
            let target = Target::new(&session.id, &stream.id);
            let gate = gates(&target);
            gate.check(None)?;
            match self.admit(target) {
                Ok(ticket) => admissions.push((ticket, gate, stream.clone())),
                Err(error) => return Err(error),
            }
        }
        let owned = self.clone();
        tokio::spawn(async move {
            let mut results = Vec::new();
            for (ticket, gate, stream) in admissions {
                match owned.start_admitted(ticket, gate, false).await {
                    Ok(worker) => results.push(worker),
                    Err(DeviceError::Cancelled | DeviceError::Closed) => {
                        return Err(DeviceError::Cancelled);
                    }
                    Err(error) => {
                        let mut status = WorkerStatus::idle(&stream);
                        status.state = "error".into();
                        status.last_error = Some(error.to_string());
                        results.push(status);
                    }
                }
            }
            Ok(results)
        })
        .await
        .map_err(|_| DeviceError::Worker("Session worker start task failed".into()))?
    }
    async fn start_admitted(
        self: &Arc<Self>,
        mut ticket: Ticket,
        gate: ExecutionGate,
        paused: bool,
    ) -> DeviceResult<WorkerStatus> {
        let _lifecycle = ticket.lifecycle.lock.lock().await;
        self.check_ticket(&ticket)?;
        let session = self.session(&ticket.target.session_id).await?;
        if session.state != "running" {
            return Err(DeviceError::Worker(
                "Сначала запустите сеанс мониторинга".into(),
            ));
        }
        let stream = Self::stream(&session, &ticket.target.stream_id)?.clone();
        {
            let registry = self.registry.lock().map_err(|_| DeviceError::Closed)?;
            if let Some(worker) = registry.workers.get(&ticket.target)
                && !worker.finished_ok()
            {
                return Err(DeviceError::Worker(format!(
                    "Исполнитель уже запущен или не завершил очистку: {}",
                    stream.id
                )));
            }
            if registry
                .workers
                .values()
                .filter(|worker| !worker.finished_ok())
                .count()
                >= self.options.max_workers
            {
                return Err(DeviceError::QueueFull);
            }
        }
        let settings = stream.settings.clone().unwrap_or_default();
        let period = simulator::period(&settings)?;
        let max_errors = simulator::integer(&settings, "max_consecutive_errors", 3, 1, 100)?;
        let adapter_name = settings
            .get("adapter")
            .and_then(Value::as_str)
            .unwrap_or("");
        let (adapter, native, released, port) = if matches!(
            adapter_name,
            "tinysa-sim" | "ardv1-sim"
        ) {
            (
                Adapter::Simulated(Box::new(simulator::Simulated::new(&stream)?)),
                None,
                None,
                None,
            )
        } else if matches!(adapter_name, "tinysa-serial" | "ardv1-serial") {
            let factory=self.options.transport.clone().ok_or_else(||DeviceError::Unsupported("Physical serial is disabled; an explicit native connector and connect request are required".into()))?;
            let profile = Profile::read(&stream)?;
            let identity = factory.identity(&stream)?;
            if identity.is_empty() || identity.len() > 1024 {
                return Err(DeviceError::Invalid(
                    "Invalid bounded serial identity".into(),
                ));
            }
            {
                let mut registry = self.registry.lock().map_err(|_| DeviceError::Closed)?;
                if registry.ports.contains_key(&identity) {
                    return Err(DeviceError::Worker(
                        "Порт уже принадлежит другому исполнителю".into(),
                    ));
                }
                registry
                    .ports
                    .insert(identity.clone(), ticket.target.clone());
                ticket.pending.port = Some(identity.clone());
            }
            gate.check(None)?;
            self.check_ticket(&ticket)?;
            let transport =
                tokio::time::timeout(self.options.connect_timeout, factory.open(&stream))
                    .await
                    .map_err(|_| {
                        DeviceError::Worker("Device connection deadline exceeded".into())
                    })??;
            self.check_ticket(&ticket)?;
            gate.check(None)?;
            let (transport, released) = transport::OwnedTransport::new(transport);
            let native = match profile {
                Profile::Analyzer(config) => Native::Analyzer(TinySaActor::spawn(
                    TinySa::new(transport, config, self.options.io_limits.clone())?,
                    1,
                )?),
                Profile::Receiver(config) => Native::Receiver(ArDv1Actor::spawn(
                    ArDv1::new(transport, config, self.options.io_limits.clone())?,
                    1,
                )?),
            };
            (
                Adapter::Native(native.clone()),
                Some(native),
                Some(released),
                Some(identity),
            )
        } else {
            return Err(DeviceError::Unsupported(format!(
                "Поток {}: требуется явный адаптер tinysa-sim, ardv1-sim, tinysa-serial или ardv1-serial",
                stream.id
            )));
        };
        let (sender, receiver) = mpsc::channel(self.options.queue_capacity);
        let (priority, priority_receiver) = mpsc::channel(1);
        let instance = ticket.pending.identity.clone();
        let control = control::State::new(instance.clone(), native.as_ref(), paused);
        let worker = Arc::new(Worker {
            manager: Arc::downgrade(self),
            archive: self.archive.clone(),
            stream: stream.clone(),
            instance,
            control,
            native,
            released,
            port,
            status: Mutex::new(WorkerStatus::idle(&stream)),
            commands: sender,
            priority,
            tune_tasks: receipt::TuneTasks::new(self.options.queue_capacity),
            stopping: AtomicBool::new(false),
            active_io: AtomicBool::new(false),
            acquisition: Mutex::new(acquisition::Phases::default()),
            stop: Notify::new(),
            completion: Completion::default(),
            applied_revision: AtomicU64::new(0),
            confirmed_tunes: AtomicU64::new(0),
            preparation: Mutex::new(None),
            period,
            max_errors,
            lifecycle: ticket.lifecycle.clone(),
            start_generation: ticket.generation,
        });
        {
            let mut registry = self.registry.lock().map_err(|_| DeviceError::Closed)?;
            registry
                .workers
                .insert(ticket.target.clone(), worker.clone());
        }
        ticket.pending.port = None;
        let (initialized, ready) = oneshot::channel();
        let owned = worker.clone();
        let manager = self.clone();
        tokio::spawn(async move {
            owned
                .run(adapter, receiver, priority_receiver, gate, initialized)
                .await;
            drop(manager);
        });
        let result = ready
            .await
            .map_err(|_| DeviceError::Worker("Worker initialization failed".into()))?;
        self.check_ticket(&ticket)?;
        result?;
        Ok(worker.snapshot())
    }
    pub fn quiesce_session(self: &Arc<Self>, session: &str) -> DeviceResult<SessionStop> {
        let mut registry = self.registry.lock().map_err(|_| DeviceError::Closed)?;
        *registry.stopping.entry(session.into()).or_default() += 1;
        for (target, lifecycle) in &registry.lifecycles {
            if target.session_id == session {
                lifecycle.generation.fetch_add(1, Ordering::AcqRel);
            }
        }
        for (target, worker) in &registry.workers {
            if target.session_id == session {
                worker.request_stop();
            }
        }
        Ok(SessionStop {
            manager: self.clone(),
            session: session.into(),
        })
    }
    pub async fn stop_stream(
        self: &Arc<Self>,
        session: &str,
        stream: &str,
    ) -> DeviceResult<WorkerStatus> {
        let session_value = self.session(session).await?;
        let stream_value = Self::stream(&session_value, stream)?.clone();
        let target = Target::new(session, stream);
        let lifecycle = {
            let mut registry = self.registry.lock().map_err(|_| DeviceError::Closed)?;
            let lifecycle = registry
                .lifecycles
                .entry(target.clone())
                .or_default()
                .clone();
            lifecycle.generation.fetch_add(1, Ordering::AcqRel);
            if let Some(worker) = registry.workers.get(&target) {
                worker.request_stop();
            }
            lifecycle
        };
        let manager = self.clone();
        tokio::spawn(async move {
            let _lifecycle =
                tokio::time::timeout(manager.options.stop_timeout, lifecycle.lock.lock())
                    .await
                    .map_err(|_| {
                        DeviceError::Worker(
                            "Worker start did not drain before stop deadline".into(),
                        )
                    })?;
            let worker = manager
                .registry
                .lock()
                .map_err(|_| DeviceError::Closed)?
                .workers
                .get(&target)
                .cloned();
            if let Some(worker) = worker {
                manager
                    .stop_retained_worker(&RetainedWorkerHandle::cleanup(worker))
                    .await
            } else {
                Ok(WorkerStatus::idle(&stream_value))
            }
        })
        .await
        .map_err(|_| DeviceError::Worker("Worker stop task failed".into()))?
    }
    pub async fn stop_session(self: &Arc<Self>, session: &str) -> DeviceResult<Vec<WorkerStatus>> {
        let _scope = self.quiesce_session(session)?;
        self.stop_session_quiesced(session).await
    }
    pub async fn stop_session_quiesced(
        self: &Arc<Self>,
        session: &str,
    ) -> DeviceResult<Vec<WorkerStatus>> {
        let session = self.session(session).await?;
        let results = futures_util::future::join_all(
            session
                .streams
                .iter()
                .map(|stream| self.stop_stream(&session.id, &stream.id)),
        )
        .await;
        results.into_iter().collect()
    }
    pub async fn list(&self, session: Option<&str>) -> DeviceResult<Vec<WorkerStatus>> {
        let sessions = if let Some(id) = session {
            vec![self.session(id).await?]
        } else {
            let page = self.archive.list_sessions(1000, None).await?;
            if page.next_cursor.is_some() {
                return Err(DeviceError::QueueFull);
            }
            page.items
                .iter()
                .map(parse_session)
                .collect::<DeviceResult<Vec<_>>>()?
        };
        let workers = self
            .registry
            .lock()
            .map_err(|_| DeviceError::Closed)?
            .workers
            .clone();
        Ok(sessions
            .iter()
            .flat_map(|session| {
                session.streams.iter().map(|stream| {
                    workers
                        .get(&Target::new(&session.id, &stream.id))
                        .map(|worker| worker.snapshot())
                        .unwrap_or_else(|| WorkerStatus::idle(stream))
                })
            })
            .collect())
    }
    pub async fn tune(
        self: &Arc<Self>,
        target: Target,
        frequency: u64,
        mode: Option<String>,
        gate: ExecutionGate,
    ) -> DeviceResult<TuneOutcome> {
        (gate.check)()?;
        let session = self.session(&target.session_id).await?;
        let stream = Self::stream(&session, &target.stream_id)?;
        if stream.role != DeviceRole::Receiver {
            return Err(DeviceError::Worker(
                "Перестройка доступна только потоку receiver".into(),
            ));
        }
        let retained: RetainedWorkerHandle = self.capture_worker(&target, gate)?;
        let worker = retained.original_worker();
        let gate = retained.execution_gate();
        if retained.target() != &target
            || retained.worker_instance() != worker.instance
            || retained.connection_id() != worker.instance
        {
            return Err(DeviceError::Denied);
        }
        if mode.as_ref().is_some_and(|mode| mode.len() > 64) {
            return Err(DeviceError::Invalid(
                "Receiver mode exceeds 64 bytes".into(),
            ));
        }
        let caller = worker.tune_tasks.enqueue(
            worker.clone(),
            frequency,
            mode,
            gate,
            self.options.stop_timeout,
        )?;
        caller
            .await
            .map_err(|_| DeviceError::Worker("Original tune monitor did not complete".into()))?
    }

    pub async fn close(self: &Arc<Self>) -> DeviceResult<()> {
        self.request_shutdown()?;
        let targets = {
            let registry = self.registry.lock().map_err(|_| DeviceError::Closed)?;
            registry.lifecycles.keys().cloned().collect::<Vec<_>>()
        };
        let results = futures_util::future::join_all(
            targets
                .iter()
                .map(|target| self.stop_stream(&target.session_id, &target.stream_id)),
        )
        .await;
        for result in results {
            result?;
        }
        Ok(())
    }
}
struct PendingStart {
    manager: Arc<DeviceManager>,
    target: Target,
    identity: String,
    port: Option<String>,
}
impl Drop for PendingStart {
    fn drop(&mut self) {
        if let Ok(mut registry) = self.manager.registry.lock() {
            if registry.pending.get(&self.target) != Some(&self.identity) {
                return;
            }
            registry.pending.remove(&self.target);
            if let Some(port) = &self.port
                && registry.ports.get(port) == Some(&self.target)
            {
                registry.ports.remove(port);
            }
        }
    }
}
pub(crate) struct SessionStop {
    manager: Arc<DeviceManager>,
    session: String,
}
impl Drop for SessionStop {
    fn drop(&mut self) {
        if let Ok(mut registry) = self.manager.registry.lock()
            && let Some(count) = registry.stopping.get_mut(&self.session)
        {
            *count -= 1;
            if *count == 0 {
                registry.stopping.remove(&self.session);
            }
        }
    }
}

enum Profile {
    Analyzer(ReceiveConfig),
    Receiver(ReceiverConfig),
}
impl Profile {
    fn read(stream: &DeviceStream) -> DeviceResult<Self> {
        let s = stream.settings.clone().unwrap_or_default();
        match s.get("adapter").and_then(Value::as_str) {
            Some("tinysa-serial") => {
                if s.get("receive_profile").is_some_and(|profile| {
                    profile.as_str() != Some(ori_instruments::RECEIVE_PROFILE)
                }) {
                    return Err(DeviceError::Invalid(
                        "Unknown tinySA receive profile".into(),
                    ));
                }
                if stream.role != DeviceRole::SpectrumAnalyzer || stream.level_unit != "dBm" {
                    return Err(DeviceError::Invalid(
                        "tinysa-serial requires spectrum_analyzer in raw dBm".into(),
                    ));
                }
                let config = ReceiveConfig {
                    start_hz: simulator::integer(&s, "start_hz", 88_000_000, 100_000, 800_000_000)?,
                    stop_hz: simulator::integer(&s, "stop_hz", 108_000_000, 100_000, 800_000_000)?,
                    points: simulator::integer(&s, "points", 201, 2, 290)? as u32,
                    rbw_hz: simulator::integer(&s, "rbw_hz", 10_000, 10_000, 30_000)? as u32,
                };
                if config.stop_hz <= config.start_hz || !matches!(config.rbw_hz, 10_000 | 30_000) {
                    return Err(DeviceError::Invalid(
                        "Receive configuration violates verified tinySA profile".into(),
                    ));
                }
                Ok(Self::Analyzer(config))
            }
            Some("ardv1-serial") => {
                if stream.role != DeviceRole::Receiver || stream.level_unit != "s_meter_count" {
                    return Err(DeviceError::Invalid(
                        "ardv1-serial requires receiver in raw s_meter_count".into(),
                    ));
                }
                let profile = match s.get("receiver_profile").filter(|v| !v.is_null()) {
                    None => ReceiverProfile::LegacyFmAm,
                    Some(Value::String(profile)) if profile == "ardv1-monitor-v1" => {
                        ReceiverProfile::MonitorV1
                    }
                    _ => return Err(DeviceError::Invalid("Неизвестный профиль AR-DV1".into())),
                };
                let frequency =
                    simulator::integer(&s, "frequency_hz", 145_500_000, 100_000, 1_300_000_000)?;
                if !frequency.is_multiple_of(10) {
                    return Err(DeviceError::Invalid(
                        "Частота AR-DV1 должна быть кратна 10 Гц".into(),
                    ));
                }
                let mode = s
                    .get("mode")
                    .and_then(Value::as_str)
                    .filter(|v| !v.is_empty())
                    .unwrap_or("FM")
                    .parse::<ReceiverMode>()?;
                if profile == ReceiverProfile::LegacyFmAm
                    && !matches!(mode, ReceiverMode::Fm | ReceiverMode::Am)
                {
                    return Err(DeviceError::Invalid(
                        "Режим не поддерживается выбранным профилем AR-DV1".into(),
                    ));
                }
                Ok(Self::Receiver(ReceiverConfig {
                    frequency_hz: frequency,
                    mode,
                    profile,
                }))
            }
            _ => Err(DeviceError::Unsupported(
                "Explicit serial adapter required".into(),
            )),
        }
    }
}

#[derive(Default)]
struct Completion {
    result: Mutex<Option<std::result::Result<(), String>>>,
    notify: Notify,
}
impl Completion {
    fn finish(&self, result: DeviceResult<()>) {
        if let Ok(mut slot) = self.result.lock() {
            *slot = Some(result.map_err(|error| error.to_string()));
        }
        self.notify.notify_waiters();
    }
    async fn wait(&self) -> DeviceResult<()> {
        loop {
            let notified = self.notify.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            if let Some(result) = self.result.lock().map_err(|_| DeviceError::Closed)?.clone() {
                return result.map_err(DeviceError::Worker);
            }
            notified.await;
        }
    }
    fn okay(&self) -> bool {
        self.result
            .lock()
            .is_ok_and(|result| matches!(*result, Some(Ok(()))))
    }
}
struct Worker {
    manager: Weak<DeviceManager>,
    archive: Archive,
    stream: DeviceStream,
    instance: String,
    native: Option<Native>,
    released: Option<Arc<transport::Released>>,
    port: Option<String>,
    status: Mutex<WorkerStatus>,
    commands: mpsc::Sender<Command>,
    priority: mpsc::Sender<control::Request>,
    control: control::State,
    tune_tasks: Arc<receipt::TuneTasks>,
    stopping: AtomicBool,
    active_io: AtomicBool,
    acquisition: Mutex<acquisition::Phases>,
    stop: Notify,
    completion: Completion,
    applied_revision: AtomicU64,
    confirmed_tunes: AtomicU64,
    preparation: Mutex<Option<PrepareReport>>,
    period: Duration,
    max_errors: u64,
    lifecycle: Arc<Lifecycle>,
    start_generation: u64,
}
#[derive(Clone)]
pub struct TuneOutcome {
    pub tuned: Value,
    pub worker: WorkerStatus,
    pub acknowledged_at: String,
    pub applied_tune_revision: u64,
    original: Weak<Worker>,
    gate: ExecutionGate,
}
impl TuneOutcome {
    pub fn revalidate(&self) -> DeviceResult<()> {
        let worker = self.original.upgrade().ok_or(DeviceError::Denied)?;
        self.gate.check(Some(&worker))?;
        worker.validate_tune(self.applied_tune_revision, &self.tuned)
    }
    pub fn value(&self) -> DeviceResult<Value> {
        Ok(json!({"tuned":self.tuned,"worker":self.worker.value()?}))
    }
}
impl Worker {
    fn finish(&self, result: DeviceResult<()>) {
        if let Ok(mut status) = self.status.lock() {
            status.alive = false;
            status.ended_at = Some(crate::utc_now());
            if let Err(error) = &result {
                status.state = "error".into();
                status.last_error = Some(error.to_string());
            }
        }
        self.completion.finish(result);
    }
    fn finished_ok(&self) -> bool {
        self.completion.okay() && self.tune_tasks.okay()
    }
    fn snapshot(&self) -> WorkerStatus {
        let mut status = self.status.lock().expect("worker status poisoned").clone();
        if let Some(native) = &self.native {
            status.current = native.current();
            if let Some(report) = self.preparation() {
                status.current["generator_output"] = json!("off");
                status.current["generator_output_source"] =
                    json!("command_acknowledged_no_physical_readback");
                status.current["device_mode"] = json!("input");
                status.current["device_mode_readback"] = json!(false);
                status.current["command_preparation"] =
                    value(&report).expect("fixed finite preparation report");
            }
            if matches!(native, Native::Receiver(_)) {
                status.current["tune_sequence"] =
                    self.confirmed_tunes.load(Ordering::Acquire).into();
            }
            self.control.project(Some(native), &mut status.current);
        }
        status.worker_instance = Some(self.instance.clone());
        status
    }
    fn preparation(&self) -> Option<PrepareReport> {
        if self.live().is_err() {
            return None;
        }
        let Some(Native::Analyzer(actor)) = &self.native else {
            return None;
        };
        let state = actor.state();
        if !state.profile_verified
            || state.restart_required
            || state.closed
            || state.control_mode != ori_instruments::ControlMode::Monitoring
            || state.device_mode != ori_instruments::DeviceMode::Input
            || state.settings_source != "command_acknowledged"
        {
            return None;
        }
        self.preparation.lock().ok()?.clone()
    }
    fn request_stop(&self) {
        self.stopping.store(true, Ordering::Release);
        self.tune_tasks.begin_close();
        self.stop.notify_waiters();
        if self.active_io.load(Ordering::Acquire)
            && let Some(native) = &self.native
        {
            native.cancel();
        }
    }
    fn live(&self) -> DeviceResult<()> {
        if self.stopping.load(Ordering::Acquire)
            || self.lifecycle.generation.load(Ordering::Acquire) != self.start_generation
        {
            Err(DeviceError::Cancelled)
        } else {
            Ok(())
        }
    }
    async fn stopped(&self) {
        loop {
            let notified = self.stop.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            if self.stopping.load(Ordering::Acquire) {
                return;
            }
            notified.await;
        }
    }
    async fn state(&self, state: &str) -> DeviceResult<()> {
        let coordinator = self.archive.domain_coordinator();
        let _order = coordinator.lock().await;
        let mut session = self
            .archive
            .get_session(&self.stream.session_id)
            .await?
            .ok_or_else(|| DeviceError::Worker("Session disappeared".into()))?;
        if matches!(state, "ready" | "running") {
            self.live()?;
            if session["state"] != "running" {
                return Err(DeviceError::Cancelled);
            }
        }
        let stream = session["streams"]
            .as_array_mut()
            .and_then(|streams| {
                streams
                    .iter_mut()
                    .find(|stream| stream["id"] == self.stream.id)
            })
            .ok_or_else(|| DeviceError::Worker("Worker stream disappeared".into()))?;
        if stream["state"] != state {
            stream["state"] = json!(state);
            let event = EventDraft::now(
                "device.state",
                Some(self.stream.session_id.clone()),
                json!({"stream_id":self.stream.id,"state":state}),
            );
            self.archive.save_session(session, Some(event)).await?;
        }
        self.status.lock().map_err(|_| DeviceError::Closed)?.state = state.into();
        Ok(())
    }
    fn actor_gate(
        self: &Arc<Self>,
        gate: ExecutionGate,
        tune: bool,
        stage: Option<Arc<receipt::TuneStage>>,
    ) -> ori_instruments::ExecutionCheck {
        let worker = self.clone();
        let began = AtomicBool::new(false);
        Arc::new(move || {
            worker
                .live()
                .and_then(|()| gate.check(Some(&worker)))
                .map_err(|_| ori_instruments::InstrumentError::PolicyDenied)?;
            if tune && !began.swap(true, Ordering::AcqRel) {
                if stage.as_ref().is_some_and(|stage| !stage.begin_io()) {
                    return Err(ori_instruments::InstrumentError::PolicyDenied);
                }
                worker.applied_revision.fetch_add(1, Ordering::AcqRel);
            }
            Ok(())
        })
    }
    async fn prepare(
        self: &Arc<Self>,
        adapter: &mut Adapter,
        gate: &ExecutionGate,
    ) -> DeviceResult<()> {
        self.live()?;
        gate.check(Some(self))?;
        match adapter {
            Adapter::Simulated(simulator) => {
                self.status.lock().map_err(|_| DeviceError::Closed)?.current = simulator.details();
                Ok(())
            }
            Adapter::Native(Native::Analyzer(actor)) => {
                let request = actor.enqueue_checked(
                    TinySaRequest::Prepare,
                    self.actor_gate(gate.clone(), false, None),
                )?;
                if !matches!(request.result().await?, TinySaReply::Ready) {
                    return Err(DeviceError::Worker("Unexpected safe-prepare reply".into()));
                }
                if self
                    .stream
                    .settings
                    .as_ref()
                    .and_then(|settings| settings.get("monitoring_instrument_context"))
                    .is_some_and(|value| value == "inspect")
                {
                    let inspection = actor
                        .enqueue_checked(
                            TinySaRequest::Inspect,
                            self.actor_gate(gate.clone(), false, None),
                        )?
                        .result()
                        .await?;
                    if !matches!(inspection, TinySaReply::Inspection(_)) {
                        return Err(DeviceError::Worker("Unexpected inspect reply".into()));
                    }
                }
                let state = actor.state();
                if !state.profile_verified
                    || state.restart_required
                    || state.closed
                    || state.device_mode != ori_instruments::DeviceMode::Input
                    || state.settings_source != "command_acknowledged"
                {
                    return Err(DeviceError::Worker(
                        "Safe prepare did not confirm input mode".into(),
                    ));
                }
                *self.preparation.lock().map_err(|_| DeviceError::Closed)? = Some(PrepareReport {
                    profile: ori_instruments::RECEIVE_PROFILE.into(),
                    acknowledged_at: crate::utc_now(),
                    input_mode_acknowledged: true,
                    generator_off_acknowledged: true,
                    device_mode_readback: false,
                    generator_output_readback: false,
                    hardware_validation: "not_run".into(),
                });
                Ok(())
            }
            Adapter::Native(Native::Receiver(actor)) => {
                let config = match Profile::read(&self.stream)? {
                    Profile::Receiver(config) => config,
                    _ => unreachable!(),
                };
                let request = actor.enqueue_checked(
                    ArDv1Request::Tune {
                        frequency_hz: config.frequency_hz,
                        mode: config.mode,
                    },
                    self.actor_gate(gate.clone(), true, None),
                )?;
                request.result().await?;
                self.confirmed_tunes.fetch_add(1, Ordering::AcqRel);
                Ok(())
            }
        }
    }
    async fn poll(self: &Arc<Self>, adapter: &mut Adapter) -> DeviceResult<Value> {
        self.live()?;
        match adapter {
            Adapter::Simulated(simulator) => simulator.poll(&self.stream),
            Adapter::Native(Native::Analyzer(actor)) => {
                let worker = self.clone();
                match actor
                    .enqueue_guarded(TinySaRequest::Poll, move || {
                        worker
                            .live()
                            .map_err(|_| ori_instruments::InstrumentError::PolicyDenied)
                    })?
                    .result()
                    .await?
                {
                    TinySaReply::Spectrum(sample) => {
                        if sample.kind != ori_instruments::SpectrumKind::RawSpectrum
                            || sample.level_unit != "dBm"
                        {
                            return Err(DeviceError::NonRawMonitoring(Box::new(sample)));
                        }
                        let mut result = value(&sample)?;
                        let object = result.as_object_mut().expect("spectrum object");
                        let state = actor.state();
                        let context = state.control_profile_verified.then(|| {
                            ori_instruments::TinySaProvenanceContext {
                                connection_id: self.instance.clone(),
                                revision: self.control.revision(),
                            }
                        });
                        let settings = ori_instruments::legacy_monitoring_settings(
                            &sample,
                            &state,
                            context.as_ref(),
                        )?;
                        object.insert("settings".into(), settings);
                        object.remove("kind");
                        object.insert("id".into(), crate::new_id().into());
                        object.insert("session_id".into(), self.stream.session_id.clone().into());
                        object.insert("stream_id".into(), self.stream.id.clone().into());
                        Ok(result)
                    }
                    _ => Err(DeviceError::Worker("Unexpected spectrum reply".into())),
                }
            }
            Adapter::Native(Native::Receiver(actor)) => {
                let worker = self.clone();
                match actor
                    .enqueue_guarded(ArDv1Request::Poll, move || {
                        worker
                            .live()
                            .map_err(|_| ori_instruments::InstrumentError::PolicyDenied)
                    })?
                    .result()
                    .await?
                {
                    ArDv1Reply::Observation(sample) => Ok(
                        json!({"id":crate::new_id(),"session_id":self.stream.session_id,"stream_id":self.stream.id,"interval":value(&sample.interval)?,"frequency_hz":sample.frequency_hz,"mode":sample.mode,"level":sample.level,"level_unit":sample.level_unit,"squelch_open":sample.squelch_open,"decoded_mode":sample.decoded_mode,"metadata":sample.metadata}),
                    ),
                    _ => Err(DeviceError::Worker("Unexpected receiver reply".into())),
                }
            }
        }
    }
    async fn apply_tune(
        self: &Arc<Self>,
        adapter: &mut Adapter,
        frequency: u64,
        mode: Option<String>,
        gate: ExecutionGate,
        stage: Arc<receipt::TuneStage>,
    ) -> DeviceResult<TuneOutcome> {
        self.live()?;
        gate.check(Some(self))?;
        let tuned = match adapter {
            Adapter::Simulated(simulator) => {
                if !stage.begin_io() {
                    return Err(DeviceError::Denied);
                }
                let tuned = simulator.tune(frequency, mode.as_deref())?;
                self.applied_revision.fetch_add(1, Ordering::AcqRel);
                self.status.lock().map_err(|_| DeviceError::Closed)?.current = tuned.clone();
                tuned
            }
            Adapter::Native(Native::Receiver(actor)) => {
                let mode = mode
                    .as_deref()
                    .filter(|v| !v.is_empty())
                    .map(str::parse::<ReceiverMode>)
                    .transpose()?
                    .or(actor.state().mode)
                    .unwrap_or(ReceiverMode::Fm);
                let request = actor.enqueue_checked(
                    ArDv1Request::Tune {
                        frequency_hz: frequency,
                        mode,
                    },
                    self.actor_gate(gate.clone(), true, Some(stage)),
                )?;
                request.result().await?;
                self.confirmed_tunes.fetch_add(1, Ordering::AcqRel);
                self.snapshot().current
            }
            Adapter::Native(Native::Analyzer(_)) => {
                return Err(DeviceError::Worker(
                    "Перестройка доступна только потоку receiver".into(),
                ));
            }
        };
        self.live()?;
        gate.check(Some(self))?;
        // Only an acknowledged tune is published. Evidence domains revalidate
        // the original epoch/instance/revision again under their coordinator.
        self.archive
            .apply(vec![Mutation::Event(EventDraft::now(
                "device.tuned",
                Some(self.stream.session_id.clone()),
                {
                    let mut payload = tuned.as_object().cloned().unwrap_or_default();
                    payload.insert("stream_id".into(), self.stream.id.clone().into());
                    Value::Object(payload)
                },
            ))])
            .await?;
        self.status
            .lock()
            .map_err(|_| DeviceError::Closed)?
            .commands_completed += 1;
        Ok(TuneOutcome {
            tuned,
            worker: self.snapshot(),
            acknowledged_at: crate::utc_now(),
            applied_tune_revision: self.applied_revision.load(Ordering::Acquire),
            original: Arc::downgrade(self),
            gate,
        })
    }
    fn validate_tune(&self, revision: u64, tuned: &Value) -> DeviceResult<()> {
        self.live()?;
        let status = self.snapshot();
        if !status.alive
            || self.applied_revision.load(Ordering::Acquire) != revision
            || status.current["frequency_hz"] != tuned["frequency_hz"]
            || status.current["mode"] != tuned["mode"]
            || status.current["frequency_hz"].is_null()
            || status.current["restart_required"] == true
        {
            return Err(DeviceError::Denied);
        }
        Ok(())
    }
    async fn cleanup(self: &Arc<Self>, adapter: Adapter) -> DeviceResult<()> {
        self.stopping.store(true, Ordering::Release);
        self.tune_tasks.begin_close();
        let intent = self.instrument_cleanup_barrier().await;
        if let Adapter::Native(native) = adapter {
            let result = native.close().await;
            if let Some(released) = &self.released {
                released.wait().await;
            }
            result?;
        }
        intent?;
        let terminal = if self.status.lock().map_err(|_| DeviceError::Closed)?.state == "error" {
            "error"
        } else {
            "disconnected"
        };
        self.state(terminal).await?;
        let mut status = self.status.lock().map_err(|_| DeviceError::Closed)?;
        status.alive = false;
        status.ended_at = Some(crate::utc_now());
        drop(status);
        if let Some(manager) = self.manager.upgrade()
            && let Some(port) = &self.port
        {
            let mut registry = manager.registry.lock().map_err(|_| DeviceError::Closed)?;
            if registry.ports.get(port)
                == Some(&Target::new(&self.stream.session_id, &self.stream.id))
            {
                registry.ports.remove(port);
            }
        }
        Ok(())
    }
    async fn run(
        self: Arc<Self>,
        mut adapter: Adapter,
        mut commands: mpsc::Receiver<Command>,
        mut priority: mpsc::Receiver<control::Request>,
        gate: ExecutionGate,
        initialized: oneshot::Sender<DeviceResult<()>>,
    ) {
        {
            let mut status = self.status.lock().expect("worker status poisoned");
            status.alive = true;
            status.state = "connecting".into();
            status.started_at = Some(crate::utc_now());
        }
        self.active_io.store(true, Ordering::Release);
        let prepare = self.prepare(&mut adapter, &gate).await;
        self.active_io.store(false, Ordering::Release);
        let prepare = match prepare {
            Ok(()) => self.state("ready").await,
            Err(error) => Err(error),
        };
        if let Err(error) = prepare {
            let message = error.to_string();
            self.status
                .lock()
                .expect("worker status poisoned")
                .last_error = Some(message);
            let _ = self.state("error").await;
            let cleaned = self.cleanup(adapter).await;
            self.finish(cleaned);
            let _ = initialized.send(Err(error));
            return;
        }
        if let Err(error) = self.state("running").await {
            let cleaned = self.cleanup(adapter).await;
            self.finish(cleaned);
            let _ = initialized.send(Err(error));
            return;
        }
        self.control.refresh(self.native.as_ref());
        self.acquisition_ready();
        let _ = initialized.send(Ok(()));
        'running: loop {
            if self.stopping.load(Ordering::Acquire) {
                break;
            }
            if let Ok(command) = priority.try_recv() {
                self.apply_priority(command).await;
                if self.stopping.load(Ordering::Acquire) {
                    break;
                }
            }
            // FIFO, bounded commands. Polling stays live during a receiver
            // manual lease; only requested control effects check that lease.
            for _ in 0..8 {
                let command = match commands.try_recv() {
                    Ok(command) => command,
                    Err(_) => break,
                };
                self.apply_command(&mut adapter, command).await;
                if self.stopping.load(Ordering::Acquire) {
                    break 'running;
                }
            }
            if self.control.paused()
                || matches!(&self.native,Some(Native::Analyzer(actor))if actor.state().control_mode==ori_instruments::ControlMode::Lab)
            {
                tokio::select! {biased; _=self.stopped()=>break,command=priority.recv()=>{match command{Some(command)=>self.apply_priority(command).await,None=>break}},command=commands.recv()=>{match command{Some(command)=>self.apply_command(&mut adapter,command).await,None=>break}},_=tokio::time::sleep(Duration::from_millis(25))=>{}}
                continue;
            }
            self.active_io.store(true, Ordering::Release);
            self.acquisition_poll_started();
            let result = self.poll(&mut adapter).await;
            self.acquisition_poll_finished();
            self.active_io.store(false, Ordering::Release);
            if self.stopping.load(Ordering::Acquire) {
                break;
            }
            let committed = match result {
                Err(DeviceError::NonRawMonitoring(sample)) => {
                    if let Err(error) = self.pause_non_raw_monitoring(*sample).await {
                        let mut status = self.status.lock().expect("worker status poisoned");
                        status.last_error = Some(error.to_string());
                        status.state = "error".into();
                        break;
                    }
                    continue;
                }
                Ok(measurement) => {
                    self.acquisition_commit_started();
                    let committed = if self.stream.role == DeviceRole::SpectrumAnalyzer {
                        self.archive.append_sweeps(vec![measurement]).await
                    } else {
                        self.archive.append_observations(vec![measurement]).await
                    };
                    self.acquisition_commit_finished(committed.is_ok());
                    committed.map_err(DeviceError::from)
                }
                Err(error) => Err(error),
            };
            match committed {
                Ok(committed) => {
                    let source = &committed.items[0];
                    {
                        let mut status = self.status.lock().expect("worker status poisoned");
                        status.measurements += 1;
                        status.consecutive_errors = 0;
                        status.last_measurement_id = source["id"].as_str().map(str::to_owned);
                        status.last_measurement_at =
                            source["interval"]["ended_at"].as_str().map(str::to_owned);
                    }
                    if self.snapshot().state != "running" && self.state("running").await.is_err() {
                        break;
                    }
                }
                Err(error) => {
                    let fatal = matches!(error, DeviceError::Archive(_))
                        || self
                            .native
                            .as_ref()
                            .is_some_and(|native| native.current()["restart_required"] == true);
                    let message = error.to_string();
                    let count = {
                        let mut status = self.status.lock().expect("worker status poisoned");
                        status.consecutive_errors += 1;
                        status.last_error = Some(message.clone());
                        status.consecutive_errors
                    };
                    if self.state("degraded").await.is_err() {
                        break;
                    }
                    let event = EventDraft::now(
                        "device.error",
                        Some(self.stream.session_id.clone()),
                        json!({"stream_id":self.stream.id,"adapter":self.stream.settings.as_ref().and_then(|s|s.get("adapter")),"error":message,"consecutive_errors":count}),
                    );
                    if self
                        .archive
                        .apply(vec![Mutation::Event(event)])
                        .await
                        .is_err()
                    {
                        break;
                    }
                    if fatal || count >= self.max_errors {
                        let _ = self.state("error").await;
                        break;
                    }
                }
            }
            if self.period.is_zero() {
                tokio::task::yield_now().await;
            } else {
                tokio::select! {biased; _=self.stopped()=>break,command=priority.recv()=>{match command{Some(command)=>self.apply_priority(command).await,None=>break}},command=commands.recv()=>{match command{Some(command)=>self.apply_command(&mut adapter,command).await,None=>break}},_=tokio::time::sleep(self.period)=>{}}
            }
        }
        commands.close();
        priority.close();
        while let Ok(command) = commands.try_recv() {
            Self::reject_command(command);
        }
        while let Ok(command) = priority.try_recv() {
            Self::reject_priority(command);
        }
        let result = self.cleanup(adapter).await;
        self.finish(result);
    }
}

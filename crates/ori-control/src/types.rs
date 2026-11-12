use ori_instruments::{
    ArDv1Reply, ArDv1Request, ExecutionCheck, GeneratorSweepConfig, MarkerCommand,
    OperationInterval, RawSpectrumSample, Setting, TinySaReply, TinySaRequest, TraceCommand,
};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{collections::BTreeSet, future::Future, pin::Pin, sync::Arc};

#[derive(thiserror::Error)]
pub enum ControlError {
    #[error("invalid instrument request: {0}")]
    Invalid(&'static str),
    #[error("instrument conflict: {0}")]
    Conflict(&'static str),
    #[error("instrument operation not found")]
    NotFound,
    #[error("instrument authority changed; operation denied")]
    Denied,
    #[error("Требуется действующая эксклюзивная lab-аренда")]
    LabLeaseRequired,
    #[error("instrument backend/profile unavailable: {0}")]
    Unavailable(&'static str),
    #[error("instrument outcome is unknown; reconnect required")]
    Indeterminate(String),
    /// Backend implementations must sanitize text and never include credentials.
    #[error("instrument operation failed")]
    Backend(String),
    #[error("instrument result was not committed")]
    Persistence(String),
    #[error("instrument coordinator is closed")]
    Closed,
}
impl std::fmt::Debug for ControlError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        std::fmt::Display::fmt(self, f)
    }
}
pub type Result<T> = std::result::Result<T, ControlError>;
pub type ControlFuture<'a, T> = Pin<Box<dyn Future<Output = Result<T>> + Send + 'a>>;

/// Core supplies the actual post-error cache taint flag from the SAME adapter.
/// A policy denial before any I/O is distinct from an interrupted exchange.
pub fn classify_adapter_error(
    error: ori_instruments::InstrumentError,
    restart_required: bool,
) -> ControlError {
    use ori_instruments::InstrumentError;
    if restart_required {
        return ControlError::Indeterminate(
            "owned instrument exchange is incomplete; reconnect required".into(),
        );
    }
    match error {
        InstrumentError::InvalidRequest(_) => {
            ControlError::Invalid("request outside verified instrument profile")
        }
        InstrumentError::UnsupportedProfile(_) | InstrumentError::UnsupportedCommand(_) => {
            ControlError::Unavailable("selected_verified_profile_command_unavailable")
        }
        InstrumentError::UnsafeState(_) => ControlError::Conflict(
            "instrument state requires explicit preparation or a valid lab lease",
        ),
        InstrumentError::PolicyDenied => ControlError::Denied,
        InstrumentError::QueueFull => {
            ControlError::Conflict("owned instrument executor queue full; no command accepted")
        }
        InstrumentError::ActorStopped | InstrumentError::Closed => {
            ControlError::Unavailable("owned_instrument_not_connected")
        }
        _ => {
            ControlError::Indeterminate("owned instrument exchange failed; state is unknown".into())
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct Target {
    pub session_id: String,
    pub stream_id: String,
}
/// Implemented only by the trusted backend (and owned test backends). It retains
/// the actual original Core gate, never a lookup of the latest permission.
pub trait OwnerAuthority: Send + Sync + 'static {
    fn target(&self) -> &Target;
    fn worker_instance(&self) -> &str;
    fn connection_id(&self) -> &str;
    fn revalidate(&self) -> Result<()>;
}
/// Opaque admission authority; intentionally neither Debug nor Serialize.
#[derive(Clone)]
pub struct OwnerReceipt {
    authority: Arc<dyn OwnerAuthority>,
    target: Target,
    worker_instance: String,
    connection_id: String,
}
impl OwnerReceipt {
    /// Opaque identity of this exact admission, including its original backend
    /// authority. Equal device strings do not make two admissions equivalent.
    pub fn same_admission(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.authority, &other.authority)
    }
    /// A backend can retain bounded lookup cells without retaining completed
    /// operations, credentials or obsolete workers indefinitely.
    pub fn downgrade(&self) -> WeakOwnerReceipt {
        WeakOwnerReceipt {
            authority: Arc::downgrade(&self.authority),
            target: self.target.clone(),
            worker_instance: self.worker_instance.clone(),
            connection_id: self.connection_id.clone(),
        }
    }
    /// The backend calls this while capturing the SAME cached worker snapshot.
    /// There is deliberately no constructor accepting public identity strings.
    pub fn from_backend(authority: Arc<dyn OwnerAuthority>) -> Result<Self> {
        authority.revalidate().map_err(|_| ControlError::Denied)?;
        let target = authority.target().clone();
        let worker_instance = authority.worker_instance().to_owned();
        let connection_id = authority.connection_id().to_owned();
        if target.session_id.is_empty()
            || target.stream_id.is_empty()
            || worker_instance.is_empty()
            || connection_id.is_empty()
        {
            return Err(ControlError::Invalid(
                "incomplete owned instrument identity",
            ));
        }
        Ok(Self {
            authority,
            target,
            worker_instance,
            connection_id,
        })
    }
    pub fn target(&self) -> &Target {
        &self.target
    }
    pub fn worker_instance(&self) -> &str {
        &self.worker_instance
    }
    pub fn connection_id(&self) -> &str {
        &self.connection_id
    }
    pub fn revalidate(&self) -> Result<()> {
        self.authority
            .revalidate()
            .map_err(|_| ControlError::Denied)
    }
}
/// Deliberately neither Debug nor Serialize. It cannot be built from IDs.
#[derive(Clone)]
pub struct WeakOwnerReceipt {
    authority: std::sync::Weak<dyn OwnerAuthority>,
    target: Target,
    worker_instance: String,
    connection_id: String,
}
impl WeakOwnerReceipt {
    pub fn matches(&self, owner: &OwnerReceipt) -> bool {
        self.authority.ptr_eq(&Arc::downgrade(&owner.authority))
    }
    pub fn upgrade(&self) -> Option<OwnerReceipt> {
        Some(OwnerReceipt {
            authority: self.authority.upgrade()?,
            target: self.target.clone(),
            worker_instance: self.worker_instance.clone(),
            connection_id: self.connection_id.clone(),
        })
    }
}
/// Bound once by Core at serialized execution start; the owner never changes.
#[derive(Clone)]
pub struct BoundExecution {
    owner: OwnerReceipt,
    control_revision: u64,
}
impl BoundExecution {
    pub fn bind(owner: OwnerReceipt, expected: Option<u64>, current: u64) -> Result<Self> {
        owner.revalidate()?;
        if expected.is_some_and(|expected| expected != current) {
            return Err(ControlError::Conflict(
                "settings changed; refresh instrument state",
            ));
        }
        Ok(Self {
            owner,
            control_revision: current,
        })
    }
    pub fn owner(&self) -> &OwnerReceipt {
        &self.owner
    }
    pub fn control_revision(&self) -> u64 {
        self.control_revision
    }
    pub fn revalidate(&self) -> Result<()> {
        self.owner.revalidate()
    }
}

macro_rules! operations {
    ($($variant:ident => $wire:literal),+ $(,)?) => {
        #[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
        pub enum OperationKind { $(#[serde(rename = $wire)] $variant),+ }
        impl OperationKind {
            pub const ALL: &'static [Self] = &[$(Self::$variant),+];
            pub const fn as_str(self) -> &'static str { match self { $(Self::$variant => $wire),+ } }
            pub fn parse(value: &str) -> Result<Self> { match value { $($wire => Ok(Self::$variant),)+ _ => Err(ControlError::Invalid("Unknown typed instrument operation")) } }
        }
    };
}
operations! {
    Inspect => "inspect", Capture => "capture", LeaseAcquire => "lease.acquire",
    LeaseRenew => "lease.renew", LeaseRelease => "lease.release", MonitorResume => "monitor.resume",
    Setting => "setting", Touch => "touch", Release => "release", Keypad => "keypad",
    MenuHome => "menu.home", SdList => "sd.list", SdRead => "sd.read", SdDelete => "sd.delete",
    PresetSave => "preset.save", PresetLoad => "preset.load", AcquireRaw => "acquire.raw",
    AcquireZero => "acquire.zero", ServiceGuide => "service.guide", Trace => "trace",
    Marker => "marker", Measurement => "measurement", PresetExport => "preset.export",
    PresetImport => "preset.import", SelfTest => "selftest.start", GeneratorSweep => "generator.sweep",
    OutputOff => "output.off", AudioInputs => "audio.inputs", AudioCapture => "audio.capture",
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum InstrumentFamily {
    TinySa,
    ArDv1,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ExecutionPriority {
    Normal,
    OutputOff,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RevisionEffect {
    Unchanged,
    AdvanceAfterAcknowledgement,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MeasurementMethod {
    Peak,
    OccupiedBandwidth,
    Bandwidth3Db,
    Centroid,
    ChannelPower,
}
impl MeasurementMethod {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Peak => "peak",
            Self::OccupiedBandwidth => "occupied_bandwidth",
            Self::Bandwidth3Db => "bandwidth_3db",
            Self::Centroid => "centroid",
            Self::ChannelPower => "channel_power",
        }
    }
}
#[derive(Clone, Debug)]
pub struct RawRange {
    pub start_hz: Option<u64>,
    pub stop_hz: Option<u64>,
    pub points: Option<u32>,
    pub zero_span: bool,
}
#[derive(Clone, Debug)]
pub enum TraceOperation {
    Read { trace: u8 },
    Control(TraceCommand),
}
#[derive(Clone, Debug)]
pub struct AudioParameters {
    pub device: u32,
    pub seconds: f64,
    pub sample_rate_hz: u32,
    pub confirm_connection: bool,
}
/// Closed parsed allowlist, not an arbitrary JSON operation or console string.
#[derive(Clone, Debug)]
pub enum Operation {
    Inspect,
    Capture {
        save: bool,
    },
    LeaseAcquire {
        acknowledge_rf: bool,
    },
    LeaseRenew,
    LeaseRelease,
    MonitorResume,
    Setting(Setting),
    Touch {
        x: u16,
        y: u16,
    },
    Release,
    Keypad {
        text: String,
    },
    MenuHome,
    SdList,
    SdRead {
        filename: String,
    },
    SdDelete {
        filename: String,
        confirm_delete: bool,
    },
    PresetSave {
        slot: u8,
        confirm: bool,
    },
    PresetLoad {
        slot: u8,
        confirm: bool,
    },
    AcquireRaw(RawRange),
    AcquireZero(RawRange),
    ServiceGuide {
        name: String,
    },
    Trace(TraceOperation),
    Marker(MarkerCommand),
    Measurement {
        method: MeasurementMethod,
        range: RawRange,
    },
    PresetExport,
    PresetImport {
        settings: Vec<Setting>,
        confirm: bool,
    },
    SelfTest {
        test: u8,
        confirm: bool,
    },
    GeneratorSweep(GeneratorSweepConfig),
    OutputOff,
    AudioInputs,
    AudioCapture(AudioParameters),
}
impl Operation {
    pub const fn kind(&self) -> OperationKind {
        match self {
            Self::Inspect => OperationKind::Inspect,
            Self::Capture { .. } => OperationKind::Capture,
            Self::LeaseAcquire { .. } => OperationKind::LeaseAcquire,
            Self::LeaseRenew => OperationKind::LeaseRenew,
            Self::LeaseRelease => OperationKind::LeaseRelease,
            Self::MonitorResume => OperationKind::MonitorResume,
            Self::Setting(_) => OperationKind::Setting,
            Self::Touch { .. } => OperationKind::Touch,
            Self::Release => OperationKind::Release,
            Self::Keypad { .. } => OperationKind::Keypad,
            Self::MenuHome => OperationKind::MenuHome,
            Self::SdList => OperationKind::SdList,
            Self::SdRead { .. } => OperationKind::SdRead,
            Self::SdDelete { .. } => OperationKind::SdDelete,
            Self::PresetSave { .. } => OperationKind::PresetSave,
            Self::PresetLoad { .. } => OperationKind::PresetLoad,
            Self::AcquireRaw(_) => OperationKind::AcquireRaw,
            Self::AcquireZero(_) => OperationKind::AcquireZero,
            Self::ServiceGuide { .. } => OperationKind::ServiceGuide,
            Self::Trace(_) => OperationKind::Trace,
            Self::Marker(_) => OperationKind::Marker,
            Self::Measurement { .. } => OperationKind::Measurement,
            Self::PresetExport => OperationKind::PresetExport,
            Self::PresetImport { .. } => OperationKind::PresetImport,
            Self::SelfTest { .. } => OperationKind::SelfTest,
            Self::GeneratorSweep(_) => OperationKind::GeneratorSweep,
            Self::OutputOff => OperationKind::OutputOff,
            Self::AudioInputs => OperationKind::AudioInputs,
            Self::AudioCapture(_) => OperationKind::AudioCapture,
        }
    }
    pub const fn priority(&self) -> ExecutionPriority {
        if matches!(self, Self::OutputOff) {
            ExecutionPriority::OutputOff
        } else {
            ExecutionPriority::Normal
        }
    }
}

/// Captured atomically by the backend; status is cached and never performs I/O.
pub struct CachedInstrument {
    pub target: Target,
    pub family: InstrumentFamily,
    pub status: Value,
    pub control_revision: u64,
    pub owner: Option<OwnerReceipt>,
    pub supported_operations: BTreeSet<OperationKind>,
    /// True only when Core's global guard currently claims this stream.
    pub guard_claimed: bool,
    pub coverage_bridge_available: bool,
    pub science_available: bool,
    pub audio_available: bool,
}
pub enum AdapterCommand {
    TinySa(TinySaRequest),
    ArDv1(ArDv1Request),
}
pub enum AdapterReply {
    TinySa(TinySaReply),
    ArDv1(ArDv1Reply),
}
pub struct ExecutedStep {
    pub reply: AdapterReply,
    pub interval: OperationInterval,
}
/// Core executes this whole operation on its SAME owned per-device command loop.
/// It binds expected_revision only there, then calls the pure typed plan helper
/// with the then-current owned state (for omitted range/preset defaults).
pub struct ExecutionRequest {
    pub operation_id: String,
    pub target: Target,
    pub owner: OwnerReceipt,
    pub expected_revision: Option<u64>,
    pub operation: Operation,
    pub check: ExecutionCheck,
}
pub struct ExecutionOutcome {
    pub execution: BoundExecution,
    pub applied_revision: u64,
    pub status: Value,
    pub steps: Vec<ExecutedStep>,
    pub interval: OperationInterval,
}
pub struct CancellationRequest {
    pub operation_id: String,
    pub owner: OwnerReceipt,
}
pub struct CancellationOutcome {
    pub original_worker_joined: bool,
    pub original_transport_dropped: bool,
    pub tainted: bool,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CoverageTransition {
    PauseRequested,
    PauseConfirmed,
    ResumeRequested,
    ResumeConfirmed,
    KeepPaused,
    LeaseExpired,
}
pub struct CoverageRequest {
    pub owner: OwnerReceipt,
    pub transition: CoverageTransition,
    pub actual_status: Option<Value>,
}
pub struct CoverageOutcome {
    pub confirmed: bool,
    pub status: Value,
}
pub struct MeasurementRequest {
    pub owner: OwnerReceipt,
    pub method: MeasurementMethod,
    pub source: RawSpectrumSample,
    pub rbw_applied_hz: f64,
}
pub struct MeasurementOutcome {
    pub result: Value,
}
pub struct AudioInput {
    pub id: u32,
    pub label: String,
    pub channels: u32,
    pub default_sample_rate_hz: u32,
}
pub struct AudioInputs {
    pub inputs: Vec<AudioInput>,
}
pub struct AudioRequest {
    pub owner: OwnerReceipt,
    /// The original serialized control execution, never a fresh status lookup.
    pub execution: BoundExecution,
    pub acknowledged_state: ori_instruments::TinySaState,
    pub acknowledged_interval: OperationInterval,
    pub parameters: AudioParameters,
    pub check: ExecutionCheck,
}
pub struct AudioOutcome {
    pub wav: Vec<u8>,
    pub interval: OperationInterval,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ArtifactKind {
    Screen,
    SdFile,
    RawScan,
    ZeroSpan,
    ProcessedTrace,
    Measurement,
    Diagnostic,
    Audio,
}
pub struct ClosedArtifact {
    pub(crate) id: String,
    pub(crate) target: Target,
    pub(crate) kind: ArtifactKind,
    pub(crate) mime_type: String,
    pub(crate) filename: String,
    pub(crate) payload: Vec<u8>,
    pub(crate) sha256: String,
    pub(crate) metadata: Value,
}
impl ClosedArtifact {
    pub fn id(&self) -> &str {
        &self.id
    }
    pub fn target(&self) -> &Target {
        &self.target
    }
    pub fn kind(&self) -> ArtifactKind {
        self.kind
    }
    pub fn mime_type(&self) -> &str {
        &self.mime_type
    }
    pub fn filename(&self) -> &str {
        &self.filename
    }
    pub fn payload(&self) -> &[u8] {
        &self.payload
    }
    pub fn sha256(&self) -> &str {
        &self.sha256
    }
    pub fn metadata(&self) -> &Value {
        &self.metadata
    }
    pub fn into_payload(self) -> Vec<u8> {
        self.payload
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OperationState {
    Queued,
    Running,
    Succeeded,
    Failed,
    Cancelled,
    Unknown,
}
impl OperationState {
    pub const fn terminal(self) -> bool {
        matches!(
            self,
            Self::Succeeded | Self::Failed | Self::Cancelled | Self::Unknown
        )
    }
}
/// Contains public fields only; the coordinator never places lease secrets here.
pub struct OperationEvent {
    pub event_type: &'static str,
    pub target: Target,
    pub operation_id: String,
    pub operation: OperationKind,
    pub state: OperationState,
    pub occurred_at: chrono::DateTime<chrono::Utc>,
    pub fields: Value,
}
pub struct ControlCommit {
    pub event: OperationEvent,
    pub artifact: Option<ClosedArtifact>,
}
pub struct CommitOutcome {
    pub event_sequence: u64,
    pub artifact_summary: Option<Value>,
}

pub trait ControlExecutor: Send + Sync + 'static {
    fn snapshot(&self, target: &Target) -> Result<CachedInstrument>;
    fn execute<'a>(&'a self, request: ExecutionRequest) -> ControlFuture<'a, ExecutionOutcome>;
    fn cancel_and_join<'a>(
        &'a self,
        request: CancellationRequest,
    ) -> ControlFuture<'a, CancellationOutcome>;
    fn coverage<'a>(&'a self, _request: CoverageRequest) -> ControlFuture<'a, CoverageOutcome> {
        Box::pin(async { Err(ControlError::Unavailable("guard_manual_coverage_bridge")) })
    }
    fn analyze<'a>(
        &'a self,
        _request: MeasurementRequest,
    ) -> ControlFuture<'a, MeasurementOutcome> {
        Box::pin(async { Err(ControlError::Unavailable("scientific_worker")) })
    }
    fn audio_inputs(&self) -> ControlFuture<'_, AudioInputs> {
        Box::pin(async { Err(ControlError::Unavailable("selected_audio_input")) })
    }
    fn capture_audio<'a>(&'a self, _request: AudioRequest) -> ControlFuture<'a, AudioOutcome> {
        Box::pin(async { Err(ControlError::Unavailable("selected_audio_input")) })
    }
}
pub trait ControlCommitter: Send + Sync + 'static {
    fn commit<'a>(&'a self, batch: ControlCommit) -> ControlFuture<'a, CommitOutcome>;
}

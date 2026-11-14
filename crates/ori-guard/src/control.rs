//! Core-owned effects. The guard domain never opens a port, audio device,
//! filesystem probe or HTTP connection. Every effect is delegated here.
use ori_contracts::{JSONObject, Session, UtcTimestamp};
use serde::{Deserialize, Serialize};
use std::{future::Future, pin::Pin};

#[derive(Debug, thiserror::Error)]
pub enum ControlError {
    #[error("control policy denied the operation")]
    Denied,
    #[error("control capability is unavailable: {0}")]
    Unavailable(&'static str),
    /// Implementations must sanitize errors: never include permit/webhook secrets.
    #[error("instrument control failed: {0}")]
    Failed(String),
}
pub type ControlResult<T> = Result<T, ControlError>;
pub type ControlFuture<'a, T> = Pin<Box<dyn Future<Output = ControlResult<T>> + Send + 'a>>;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ControlTarget {
    pub session_id: String,
    pub stream_id: String,
}

/// Ephemeral domain authorization, intentionally neither Debug nor Serialize.
/// Core must clone it into the actor execution guard and revalidate after queue
/// wait immediately before I/O. An old epoch must never become valid again when
/// a manual owner returns or a project is restarted.
#[derive(Clone)]
pub struct ControlPermit {
    pub(crate) project_id: String,
    pub(crate) token: String,
    pub(crate) epoch: u64,
    pub(crate) expected_worker_instance: Option<String>,
}
impl ControlPermit {
    pub fn project_id(&self) -> &str {
        &self.project_id
    }
    pub fn epoch(&self) -> u64 {
        self.epoch
    }
    pub fn expected_worker_instance(&self) -> Option<&str> {
        self.expected_worker_instance.as_deref()
    }
}

/// Exact legacy worker wire fields plus opaque, non-wire instance identity.
/// `extra` retains device, adapter, counters, timestamps/errors and future fields
/// without inventing absent information. Unknown current details stay unknown.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct WorkerStatus {
    pub session_id: String,
    pub stream_id: String,
    pub state: String,
    pub alive: bool,
    pub current: JSONObject,
    #[serde(default, flatten)]
    pub extra: JSONObject,
    #[serde(skip)]
    pub worker_instance: Option<String>,
}
impl ori_contracts::RestoreKeys for WorkerStatus {
    fn restore_keys(&mut self) {
        ori_contracts::RestoreKeys::restore_keys(&mut self.current);
        ori_contracts::RestoreKeys::restore_keys(&mut self.extra);
    }
}

#[derive(Clone, Debug)]
pub struct PreflightCheck {
    pub name: String,
    pub confirmed: Option<bool>,
    pub detail: String,
}
#[derive(Clone, Debug)]
pub struct InstrumentPreflight {
    /// None is unknown and must fail domain preflight, never default to true.
    pub ready: Option<bool>,
    pub checks: Vec<PreflightCheck>,
}
#[derive(Clone, Debug)]
pub struct StorageProbe {
    pub filesystem_free_bytes: Option<u64>,
    /// Core owns the actual write probe; None cannot establish write readiness.
    pub writable: Option<bool>,
}

#[derive(Clone)]
pub struct TuneRequest {
    pub target: ControlTarget,
    pub permit: ControlPermit,
    pub frequency_hz: u64,
    pub mode: String,
}
#[derive(Clone, Debug)]
pub struct TuneReply {
    pub acknowledged: Option<bool>,
    pub tuned: JSONObject,
    pub worker: WorkerStatus,
    pub acknowledged_at: UtcTimestamp,
    /// Core-internal revision advanced at actual actor execution, never enqueue.
    pub applied_tune_revision: u64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct AudioInput {
    pub index: u32,
    pub name: String,
    #[serde(default, flatten)]
    pub extra: JSONObject,
}
impl ori_contracts::RestoreKeys for AudioInput {
    fn restore_keys(&mut self) {
        ori_contracts::RestoreKeys::restore_keys(&mut self.extra);
    }
}
#[derive(Clone, Debug)]
pub struct AudioInputs {
    pub available: Option<bool>,
    pub devices: Vec<AudioInput>,
    pub error: Option<String>,
}
#[derive(Clone)]
pub struct AudioRequest {
    pub target: ControlTarget,
    pub permit: ControlPermit,
    /// Original acknowledged receiver tune. Core must bind the capture gate and
    /// not_before to this receipt, never refresh it from a later worker cache.
    pub tune_reply: TuneReply,
    pub input: AudioInput,
    pub seconds: f64,
    pub sample_rate_hz: u32,
}
#[derive(Clone, Debug)]
pub struct RecordedAudio {
    pub wav_bytes: Vec<u8>,
    pub started_at: UtcTimestamp,
    pub ended_at: UtcTimestamp,
    pub sample_rate_hz: u32,
    pub channels: u16,
}

/// Outbox transport remains core-owned. `body` is the exact UTF-8 payload to
/// sign/send using the existing Python webhook header/HMAC policy. No secrets
/// are Debug/Serialize; responses must not echo bodies or credentials.
#[derive(Clone)]
pub struct WebhookRequest {
    pub notification_id: String,
    pub url: String,
    pub body: Vec<u8>,
    pub secret: String,
}
#[derive(Clone, Debug)]
pub struct DeliveryReceipt {
    pub delivered: Option<bool>,
    pub status: Option<u16>,
}

pub trait InstrumentControl: Send + Sync + 'static {
    /// Validate supported profiles and known cached capabilities without opening
    /// devices. Configuration compatibility is not physical RF readback.
    fn preflight<'a>(&'a self, session: &'a Session) -> ControlFuture<'a, InstrumentPreflight>;
    fn list_workers<'a>(&'a self, session_id: &'a str) -> ControlFuture<'a, Vec<WorkerStatus>>;
    fn start_session<'a>(
        &'a self,
        session: &'a Session,
        permit: ControlPermit,
    ) -> ControlFuture<'a, Vec<WorkerStatus>>;
    /// Trusted domain/core cleanup; must wait for actual stop or return failure.
    fn stop_session<'a>(&'a self, session_id: &'a str) -> ControlFuture<'a, Vec<WorkerStatus>>;
    fn start_stream<'a>(
        &'a self,
        session: &'a Session,
        stream_id: &'a str,
        permit: ControlPermit,
    ) -> ControlFuture<'a, WorkerStatus>;
    fn stop_stream<'a>(&'a self, target: &'a ControlTarget) -> ControlFuture<'a, WorkerStatus>;
    fn tune<'a>(&'a self, request: TuneRequest) -> ControlFuture<'a, TuneReply>;
    /// Cache-only post-effect gate. Recheck the same worker instance, applied
    /// revision, frequency/mode and known acknowledged state under the shared
    /// domain coordinator. Do not acquire an actor, refresh a permit or do I/O.
    fn validate_tune(&self, _reply: &TuneReply) -> ControlResult<()> {
        Err(ControlError::Unavailable("cached_tune_validation"))
    }
    fn storage_probe(&self) -> ControlFuture<'_, StorageProbe>;
    fn audio_inputs(&self) -> ControlFuture<'_, AudioInputs> {
        Box::pin(async { Err(ControlError::Unavailable("audio_inputs")) })
    }
    /// Revalidate permit before opening input and during capture; cancel/discard
    /// when revoked. Guard rechecks again before persisting any evidence.
    fn record_audio<'a>(&'a self, _request: AudioRequest) -> ControlFuture<'a, RecordedAudio> {
        Box::pin(async { Err(ControlError::Unavailable("record_audio")) })
    }
    fn deliver_webhook<'a>(
        &'a self,
        _request: WebhookRequest,
    ) -> ControlFuture<'a, DeliveryReceipt> {
        Box::pin(async { Err(ControlError::Unavailable("webhook_delivery")) })
    }
}

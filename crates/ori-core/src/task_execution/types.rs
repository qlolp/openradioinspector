use ori_tasks::{TaskContext, TaskFuture, TaskResult};
use serde::Serialize;
use std::{path::PathBuf, sync::Arc, time::Duration};

/// Original enable-time Core credential, composed with immutable opt-in
/// revocation before capture. No credential or new authorization is requested.
pub trait TaskAuthority: Send + Sync + 'static {
    fn revalidate(&self) -> TaskResult<()>;
    fn revoked(&self) -> TaskFuture<'_, ()>;
}

/// Operator-owned source selection. These paths never enter a public DTO.
#[derive(Clone)]
pub enum TaskInput {
    FixedFrequencyList(PathBuf),
    Panorama(PathBuf),
}

/// Runtime-only binding, separate from the unchanged TaskBook/input formats.
#[derive(Clone)]
pub struct TaskBinding {
    pub task_name: String,
    pub input: TaskInput,
    pub session_id: String,
    pub stream_id: String,
    pub level_unit: String,
}

/// No Default: a real trusted callback and explicit mappings are mandatory.
/// CoreConfig uses Option. Absence retains the documented software scheduler;
/// it does not advertise injected measurement execution.
#[derive(Clone)]
pub struct TaskExecutionConfig {
    pub bindings: Vec<TaskBinding>,
    pub callbacks: Arc<dyn TaskExecutionCallbacks>,
    pub max_active_runs: usize,
    pub close_timeout: Duration,
}

pub trait TaskExecutionCallbacks: Send + Sync + 'static {
    /// Cache-only enable admission. Retain the ORIGINAL worker instance and
    /// Guard receipt and carry this same authority into the actual actor gate.
    fn capture(
        &self,
        binding: &TaskBinding,
        original: Arc<dyn TaskAuthority>,
    ) -> TaskResult<Arc<dyn TaskTarget>>;
}

pub trait TaskTarget: Send + Sync + 'static {
    /// Cache-only check of the captured connection/Guard generation.
    fn revalidate(&self) -> TaskResult<()>;
    /// No I/O. Return ownership before TaskRun::start can run any effect.
    fn prepare(&self, context: TaskContext) -> TaskResult<Arc<dyn TaskRun>>;
}

pub trait TaskRun: Send + Sync + 'static {
    /// Use the SAME Core coordinator/Legacy facade and original actor gate.
    /// Futures stay owned through cancellation, even if their waiter vanishes.
    fn start(&self) -> TaskFuture<'_, ()>;
    /// Confirm actual managed output with ORIGINAL source identities/units/UTC.
    fn finish(&self) -> TaskFuture<'_, TaskCompletion>;
    /// Wake queued/executing work. Never select a replacement worker by name.
    fn cancel(&self);
    /// Actual child/producer/file/pipe cleanup; safe after authority revocation.
    fn join(&self) -> TaskFuture<'_, ()>;
}

/// Internal result proof, not another TaskBook format or HTTP response.
/// Constructors/backends retain the actual source records; no IDs are minted.
#[derive(Clone, Serialize)]
pub struct TaskCompletion {
    pub session_id: String,
    pub stream_id: String,
    pub level_unit: String,
    pub source_ids: Vec<String>,
    pub interval: ori_contracts::TimeInterval,
}

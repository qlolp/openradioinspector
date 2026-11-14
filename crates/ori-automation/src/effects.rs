use ori_runtime::worker::Cancellation;
use ori_session::{SessionFuture, SessionResult, TuneCompletionCheck, TuneReceipt, TuneRequest};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::sync::Arc;

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PolicyKind {
    Detection,
    Handoff,
    Completion,
}
impl PolicyKind {
    pub(crate) fn name(self) -> &'static str {
        match self {
            Self::Detection => "automatic_detection",
            Self::Handoff => "automatic_handoff",
            Self::Completion => "automatic_completion",
        }
    }
}
/// Capture from cached Core lifecycle/worker/tune/Guard generations only.
#[derive(Clone, Debug)]
pub struct AutomationScope {
    pub session_id: String,
    pub kind: PolicyKind,
    pub stream_id: Option<String>,
}
#[derive(Clone)]
pub struct DetectionRequest {
    pub sweep: Value,
    pub threshold_dbm: f64,
    pub maximum: u16,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DetectedRegion {
    pub center_frequency_hz: u64,
    pub bandwidth_hz: u64,
    pub peak_level: f64,
    pub score: f64,
}
/// All callbacks stay owned until they return, including after cancellation.
/// Implementations must retain original admission, use the shared research
/// pool for science, and terminate/reap their own child/device work on cancel.
pub trait AutomationEffects: Send + Sync {
    /// Canonical policy publication has completed, but dispatch has not begun.
    /// Use only cached state and the exact originating receipt; no I/O, awaits,
    /// new admission, or receipt refresh is permitted in this callback.
    fn policy_committed(
        &self,
        _scope: AutomationScope,
        _original: Option<Arc<dyn TuneCompletionCheck>>,
    ) -> SessionResult<()> {
        Ok(())
    }
    fn capture(&self, scope: AutomationScope) -> SessionResult<Arc<dyn TuneCompletionCheck>>;
    /// Capture immutable execution setup only after the actual Session lookup
    /// confirms Running. Core implementations use current cached lifecycle and
    /// owned worker receipts; never perform SQL, await, or refresh a grant here.
    /// The returned check must retain the originating policy authority.
    fn setup_running(
        &self,
        _scope: AutomationScope,
        original: Arc<dyn TuneCompletionCheck>,
    ) -> SessionResult<Arc<dyn TuneCompletionCheck>> {
        original.revalidate()?;
        Ok(original)
    }
    fn detect(
        &self,
        request: DetectionRequest,
        original: Arc<dyn TuneCompletionCheck>,
        cancel: Cancellation,
    ) -> SessionFuture<'_, Vec<DetectedRegion>>;
    fn tune(
        &self,
        request: TuneRequest,
        original: Arc<dyn TuneCompletionCheck>,
        cancel: Cancellation,
    ) -> SessionFuture<'_, TuneReceipt>;
}

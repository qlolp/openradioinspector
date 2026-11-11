use crate::SessionResult;
use serde_json::Value;
use std::{future::Future, pin::Pin, sync::Arc};

pub type SessionFuture<'a, T> = Pin<Box<dyn Future<Output = SessionResult<T>> + Send + 'a>>;

#[derive(Clone, Debug)]
pub struct TuneRequest {
    pub session_id: String,
    pub stream_id: String,
    pub frequency_hz: u64,
    pub mode: Option<String>,
}
pub trait TuneCompletionCheck: Send + Sync {
    /// Cached original authorization/worker/tune generation only: no I/O.
    fn revalidate(&self) -> SessionResult<()>;
}
#[derive(Clone)]
pub struct TuneReceipt(Arc<dyn TuneCompletionCheck>);
impl TuneReceipt {
    pub fn new(check: Arc<dyn TuneCompletionCheck>) -> Self {
        Self(check)
    }
    pub fn revalidate(&self) -> SessionResult<()> {
        self.0.revalidate()
    }
}
pub trait SessionEffects: Send + Sync {
    fn tune(&self, request: TuneRequest) -> SessionFuture<'_, TuneReceipt>;
    fn validate_reference(&self, session: Value, sweep: Value) -> SessionFuture<'_, ()>;
}

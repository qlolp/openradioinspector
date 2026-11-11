use crate::{ControlTask, TaskResult};
use std::{future::Future, pin::Pin};
pub type TaskFuture<'a, T> = Pin<Box<dyn Future<Output = TaskResult<T>> + Send + 'a>>;
/// Carries the original enable epoch; any hardware adapter must retain its own
/// original Core/Guard lease and revalidate it at actual actor execution.
#[derive(Clone)]
pub struct TaskContext {
    pub epoch: u64,
    pub task: ControlTask,
}
pub trait TaskEffects: Send + Sync {
    fn on_start(&self, context: TaskContext) -> TaskFuture<'_, ()>;
    fn on_complete(&self, context: TaskContext) -> TaskFuture<'_, ()>;
}
/// Matches the existing server: software lifecycle, without hidden measurements.
pub struct NoopTaskEffects;
impl TaskEffects for NoopTaskEffects {
    fn on_start(&self, _context: TaskContext) -> TaskFuture<'_, ()> {
        Box::pin(async { Ok(()) })
    }
    fn on_complete(&self, _context: TaskContext) -> TaskFuture<'_, ()> {
        Box::pin(async { Ok(()) })
    }
}

use crate::error::{ApiError, ApiResult};
use std::sync::{Arc, Mutex};
use tokio::sync::Notify;
#[derive(Default)]
struct State {
    active: usize,
    quiescing: bool,
}
#[derive(Default)]
pub(crate) struct Admission {
    state: Mutex<State>,
    drained: Notify,
}
pub(crate) struct MutationGuard {
    admission: Arc<Admission>,
}
impl Admission {
    pub fn enter(self: &Arc<Self>) -> ApiResult<MutationGuard> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| ApiError::unavailable("runtime_state_unavailable"))?;
        if state.quiescing {
            return Err(ApiError::unavailable("runtime_quiescing"));
        }
        state.active += 1;
        Ok(MutationGuard {
            admission: self.clone(),
        })
    }
    pub fn quiesce(&self) {
        if let Ok(mut state) = self.state.lock() {
            state.quiescing = true;
        }
    }
    pub fn is_quiescing(&self) -> bool {
        self.state.lock().map_or(true, |state| state.quiescing)
    }
    pub async fn wait(&self) {
        loop {
            let notification = self.drained.notified();
            if self.state.lock().is_ok_and(|state| state.active == 0) {
                return;
            }
            notification.await;
        }
    }
}
impl Drop for MutationGuard {
    fn drop(&mut self) {
        if let Ok(mut state) = self.admission.state.lock() {
            state.active -= 1;
            self.admission.drained.notify_waiters();
        }
    }
}

//! Shared heavy admission covers Legacy preparation, child and SQL cleanup.
use crate::{
    Core,
    private_worker::{IoClass, RetainedPermit},
};
use ori_legacy::{
    LegacyCancellation, LegacyError, LegacyIoBudget, LegacyScience, LegacySciencePermit,
    ScienceFuture,
};
use ori_runtime::{
    qos::{BackgroundQos, BackgroundWorkGuard, QosError},
    worker::{Cancellation, WorkerSpec},
};
use serde_json::Value;
use std::sync::{Arc, OnceLock, Weak};
#[cfg(test)]
#[path = "worker_tests.rs"]
mod tests;

pub(super) struct Science(pub Weak<Core>);
struct Budget {
    reservation: Arc<RetainedPermit>,
    _guard: BackgroundWorkGuard,
    qos: BackgroundQos,
    cancel: Cancellation,
    runtime: tokio::runtime::Handle,
    bridge: OnceLock<tokio::task::JoinHandle<()>>,
}
impl Budget {
    fn link(&self, original: &LegacyCancellation) {
        if original.is_cancelled() {
            self.cancel.cancel();
        }
        self.bridge.get_or_init(|| {
            let original = original.clone();
            let cancel = self.cancel.clone();
            self.runtime.spawn(async move {
                original.cancelled().await;
                cancel.cancel();
            })
        });
    }
}
impl Drop for Budget {
    fn drop(&mut self) {
        // This bridge only waits on an in-memory notification. Real child,
        // filesystem and SQL work retains this Budget through its actual JOIN.
        if let Some(bridge) = self.bridge.take() {
            bridge.abort();
        }
    }
}
fn qos(error: QosError) -> LegacyError {
    match error {
        QosError::Busy => LegacyError::Busy,
        QosError::Pressure | QosError::Cancelled => LegacyError::Cancelled,
        QosError::InvalidCharge => {
            LegacyError::ResourceLimit("Shared background I/O accounting unavailable".into())
        }
    }
}
impl LegacyIoBudget for Budget {
    fn account(&self, mut bytes: u64, cancel: &LegacyCancellation) -> ori_legacy::Result<()> {
        self.link(cancel);
        if cancel.is_cancelled() {
            return Err(LegacyError::Cancelled);
        }
        while bytes > 0 {
            let chunk = bytes.min(8 * 1024 * 1024);
            self.qos
                .account_blocking(chunk, &self.cancel)
                .map_err(qos)?;
            if cancel.is_cancelled() {
                return Err(LegacyError::Cancelled);
            }
            bytes -= chunk;
        }
        Ok(())
    }
}
struct Permit {
    core: Weak<Core>,
    budget: Arc<Budget>,
    spec: WorkerSpec,
}
fn unavailable() -> LegacyError {
    LegacyError::Science {
        status: 503,
        code: "legacy_worker_unavailable".into(),
        message: "Legacy scientific worker is unavailable".into(),
    }
}
fn runtime(error: ori_runtime::Error) -> LegacyError {
    match error {
        ori_runtime::Error::Busy => LegacyError::Busy,
        ori_runtime::Error::Cancelled => LegacyError::Cancelled,
        ori_runtime::Error::Deadline => LegacyError::Science {
            status: 504,
            code: "legacy_timeout".into(),
            message: "Истёк срок научной обработки".into(),
        },
        ori_runtime::Error::ResourceLimit | ori_runtime::Error::OutputLimit => {
            LegacyError::ResourceLimit("Legacy worker resource budget exceeded".into())
        }
        ori_runtime::Error::Protocol(_) | ori_runtime::Error::Json(_) => {
            LegacyError::Protocol("worker envelope")
        }
        _ => unavailable(),
    }
}
impl LegacyScience for Science {
    fn try_reserve(&self) -> ori_legacy::Result<Box<dyn LegacySciencePermit>> {
        let core = self.0.upgrade().ok_or(LegacyError::Closed)?;
        if *core.shutdown.borrow() {
            return Err(LegacyError::Closed);
        }
        let spec = core
            .config
            .legacy
            .worker
            .clone()
            .or_else(|| {
                core.config.analysis_worker.clone().map(|mut spec| {
                    spec.args = vec!["-I".into(), "-m".into(), "radioinspector.legacy_rpc".into()];
                    spec
                })
            })
            .ok_or_else(unavailable)?;
        let reservation = core.research_pool.try_reserve().map_err(runtime)?;
        let background = core.background.as_ref().ok_or(LegacyError::Closed)?;
        let cancel = Cancellation::default();
        let guard = background.qos.register(cancel.clone()).map_err(qos)?;
        let budget = Arc::new(Budget {
            reservation: RetainedPermit::new(reservation),
            _guard: guard,
            qos: background.qos.clone(),
            cancel,
            runtime: core.manual_runtime.clone(),
            bridge: OnceLock::new(),
        });
        Ok(Box::new(Permit {
            core: self.0.clone(),
            budget,
            spec,
        }))
    }
}
impl LegacySciencePermit for Permit {
    fn io_budget(&self) -> Option<Arc<dyn LegacyIoBudget>> {
        Some(self.budget.clone())
    }
    fn execute(self: Box<Self>, request: Value, cancel: LegacyCancellation) -> ScienceFuture {
        Box::pin(async move {
            let core = self.core.upgrade().ok_or(LegacyError::Closed)?;
            self.budget.link(&cancel);
            let result = crate::private_worker::run_retaining(
                &core,
                self.budget.reservation.clone(),
                self.spec,
                request,
                self.budget.cancel.clone(),
                None,
                IoClass::Files,
            )
            .await
            .map_err(runtime)?;
            Ok(result.response)
        })
    }
}

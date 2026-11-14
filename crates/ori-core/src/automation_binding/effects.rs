use super::*;
use crate::devices::{ExecutionGate, Target};
use authority::{Running, device};
use ori_automation::{AutomationEffects, AutomationScope, DetectedRegion, DetectionRequest};
use ori_runtime::worker::Cancellation;
use ori_session::{SessionFuture, TuneReceipt, TuneRequest};
use std::collections::BTreeMap;

pub(super) struct Effects {
    pub core: Weak<Core>,
    pub registry: Arc<Mutex<Registry>>,
    pub closed: Arc<AtomicBool>,
}
impl AutomationEffects for Effects {
    fn policy_committed(
        &self,
        scope: AutomationScope,
        original: Option<Arc<dyn TuneCompletionCheck>>,
    ) -> SessionResult<()> {
        let original = original.ok_or_else(denied)?;
        let mut registry = self.registry.lock().map_err(|_| unavailable())?;
        let record = registry
            .policies
            .get_mut(&Key::new(&scope.session_id, scope.kind))
            .ok_or_else(denied)?;
        let matches = record.pending.as_ref().is_some_and(|pending| {
            Arc::ptr_eq(&pending.check, &original) && pending.authority.matches(&scope)
        });
        if !matches {
            return Err(denied());
        }
        // Canonical configuration has changed. A rejected new grant cannot
        // fall back to the previous client's still-live authority.
        if let Some(active) = record.active.take() {
            active.revoke();
        }
        let pending = record.pending.take().ok_or_else(denied)?;
        if let Err(error) = original.revalidate() {
            pending.authority.revoke();
            return Err(error);
        }
        if pending.authority.enabled {
            record.active = Some(pending.authority);
        }
        Ok(())
    }
    fn capture(&self, scope: AutomationScope) -> SessionResult<Arc<dyn TuneCompletionCheck>> {
        if self.closed.load(Ordering::Acquire) {
            return Err(unavailable());
        }
        let registry = self.registry.lock().map_err(|_| unavailable())?;
        let active = registry
            .policies
            .get(&Key::new(&scope.session_id, scope.kind))
            .and_then(|record| record.active.clone())
            .ok_or_else(denied)?;
        if !active.matches(&scope) {
            return Err(denied());
        }
        active.revalidate()?;
        Ok(active)
    }
    fn setup_running(
        &self,
        scope: AutomationScope,
        original: Arc<dyn TuneCompletionCheck>,
    ) -> SessionResult<Arc<dyn TuneCompletionCheck>> {
        original.revalidate()?;
        let core = self.core.upgrade().ok_or_else(unavailable)?;
        let key = Key::new(&scope.session_id, scope.kind);
        let mut registry = self.registry.lock().map_err(|_| unavailable())?;
        // Library epoch checks bracket this lookup. A newer committed map
        // entry cannot replace the original pass's authority after its await.
        original.revalidate()?;
        let policy = registry
            .policies
            .get(&key)
            .and_then(|record| record.active.clone())
            .ok_or_else(denied)?;
        if !policy.matches(&scope) {
            return Err(denied());
        }
        policy.revalidate()?;
        let session = core
            .archive
            .as_ref()
            .ok_or_else(unavailable)?
            .cached_session_lifecycle(&scope.session_id)
            .ok_or_else(denied)?;
        let mut workers = BTreeMap::new();
        if scope.kind == PolicyKind::Handoff {
            let manager = core.devices.as_ref().ok_or_else(unavailable)?;
            for (stream, grant) in &policy.grants {
                let original = original.clone();
                let lifecycle = session.clone();
                let original_policy = policy.clone();
                let gate = ExecutionGate::new(
                    move || {
                        original
                            .revalidate()
                            .map_err(|_| crate::devices::DeviceError::Denied)?;
                        original_policy
                            .revalidate()
                            .map_err(|_| crate::devices::DeviceError::Denied)?;
                        if !lifecycle.check() {
                            return Err(crate::devices::DeviceError::Denied);
                        }
                        Ok(())
                    },
                    grant.expected_worker_instance().map(str::to_owned),
                );
                // Absence is never permission: a later tune requires a handle
                // captured here. No handle is captured after scientific/RF I/O.
                if let Ok(worker) =
                    manager.capture_worker(&Target::new(&scope.session_id, stream), gate)
                {
                    workers.insert(stream.clone(), worker);
                }
            }
        }
        let running = Arc::new(Running {
            policy,
            original,
            session,
            workers,
        });
        running.revalidate()?;
        registry.prune();
        if registry.running.get(&key).and_then(Weak::upgrade).is_some() {
            return Err(SessionError::new(
                503,
                "automation_busy",
                "Политика уже выполняет независимый проход",
            ));
        }
        registry.running.insert(key, Arc::downgrade(&running));
        Ok(running)
    }
    fn detect(
        &self,
        request: DetectionRequest,
        original: Arc<dyn TuneCompletionCheck>,
        cancel: Cancellation,
    ) -> SessionFuture<'_, Vec<DetectedRegion>> {
        Box::pin(async move {
            original.revalidate()?;
            let session = request.sweep["session_id"].as_str().ok_or_else(denied)?;
            let stream = request.sweep["stream_id"].as_str().ok_or_else(denied)?;
            let running = self
                .registry
                .lock()
                .map_err(|_| unavailable())?
                .running
                .get(&Key::new(session, PolicyKind::Detection))
                .and_then(Weak::upgrade)
                .ok_or_else(denied)?;
            running.revalidate()?;
            if !running.policy.grants.contains_key(stream) {
                return Err(denied());
            }
            let core = self.core.upgrade().ok_or_else(unavailable)?;
            science::detect(&core, request, original, cancel).await
        })
    }
    fn tune(
        &self,
        request: TuneRequest,
        original: Arc<dyn TuneCompletionCheck>,
        cancel: Cancellation,
    ) -> SessionFuture<'_, TuneReceipt> {
        Box::pin(async move {
            original.revalidate()?;
            if cancel.is_cancelled() {
                return Err(denied());
            }
            let running = self
                .registry
                .lock()
                .map_err(|_| unavailable())?
                .running
                .get(&Key::new(&request.session_id, PolicyKind::Handoff))
                .and_then(Weak::upgrade)
                .ok_or_else(denied)?;
            running.revalidate()?;
            let worker = running
                .workers
                .get(&request.stream_id)
                .ok_or_else(|| {
                    SessionError::new(
                        503,
                        "worker_unavailable",
                        "Исходный исполнитель receiver не подключён",
                    )
                })?
                .clone();
            worker.check().map_err(device)?;
            let base = worker.execution_gate();
            let check = original.clone();
            let retained = running.clone();
            let gate = ExecutionGate::new(
                move || {
                    check
                        .revalidate()
                        .map_err(|_| crate::devices::DeviceError::Denied)?;
                    retained
                        .revalidate()
                        .map_err(|_| crate::devices::DeviceError::Denied)?;
                    base.authorize()
                },
                Some(worker.worker_instance().into()),
            );
            let core = self.core.upgrade().ok_or_else(unavailable)?;
            let manager = core.devices.as_ref().ok_or_else(unavailable)?;
            let outcome = manager
                .tune(
                    worker.target().clone(),
                    request.frequency_hz,
                    request.mode,
                    gate,
                )
                .await;
            original.revalidate()?;
            let outcome = outcome.map_err(device)?;
            outcome.revalidate().map_err(device)?;
            Ok(TuneReceipt::new(Arc::new(Tuned { original, outcome })))
        })
    }
}
struct Tuned {
    original: Arc<dyn TuneCompletionCheck>,
    outcome: crate::devices::TuneOutcome,
}
impl TuneCompletionCheck for Tuned {
    fn revalidate(&self) -> SessionResult<()> {
        self.original.revalidate()?;
        self.outcome.revalidate().map_err(device)
    }
}

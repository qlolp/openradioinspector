//! Original, cache-only authority for Core-owned instrument actors.
use crate::{
    ControlError, ControlResult, ControlTarget, GuardService, WorkerStatus, runtime::ProjectRuntime,
};
use serde_json::json;
use std::{
    collections::BTreeMap,
    sync::{
        Arc, OnceLock, Weak,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};

#[derive(Default)]
pub(crate) struct GrantCache(BTreeMap<(String, String), Weak<AtomicBool>>);
impl GrantCache {
    fn capture(&mut self, target: &ControlTarget) -> Arc<AtomicBool> {
        self.0.retain(|_, cell| cell.strong_count() != 0);
        let key = (target.session_id.clone(), target.stream_id.clone());
        if let Some(cell) = self.0.get(&key).and_then(Weak::upgrade)
            && !cell.load(Ordering::Acquire)
        {
            return cell;
        }
        let cell = Arc::new(AtomicBool::new(false));
        self.0.insert(key, Arc::downgrade(&cell));
        cell
    }
    pub(crate) fn claim(&mut self, target: &(String, String)) {
        if let Some(cell) = self.0.remove(target).and_then(|cell| cell.upgrade()) {
            cell.store(true, Ordering::Release);
        }
    }
}

pub(crate) struct ManualControlState {
    role: String,
    original_worker: Option<String>,
    stopped_worker: OnceLock<String>,
    bound_worker: OnceLock<String>,
    pause_confirmed: AtomicBool,
}
impl ManualControlState {
    pub(crate) fn new(role: &str, original_worker: Option<String>) -> Self {
        Self {
            role: role.into(),
            original_worker,
            stopped_worker: OnceLock::new(),
            bound_worker: OnceLock::new(),
            pause_confirmed: AtomicBool::new(false),
        }
    }
    fn expected_worker(&self) -> Option<&str> {
        self.bound_worker.get().map(String::as_str).or_else(|| {
            if self.role == "receiver" && self.stopped_worker.get().is_none() {
                self.original_worker.as_deref()
            } else {
                None
            }
        })
    }
}
#[derive(Clone)]
struct ManualIdentity {
    project: String,
    token: String,
    epoch: u64,
    state: Arc<ManualControlState>,
}
#[derive(Clone)]
enum Authority {
    Unclaimed(Arc<AtomicBool>),
    Manual(ManualIdentity),
}
/// Original authority; intentionally neither Debug nor Serialize. Core combines
/// this with its original authentication, worker/connection and revision gate.
#[derive(Clone)]
pub struct InstrumentControlGrant {
    service: Weak<GuardService>,
    target: ControlTarget,
    authority: Authority,
}
impl InstrumentControlGrant {
    pub fn target(&self) -> &ControlTarget {
        &self.target
    }
    pub fn expected_worker_instance(&self) -> Option<&str> {
        match &self.authority {
            Authority::Manual(identity) => identity.state.expected_worker(),
            Authority::Unclaimed(_) => None,
        }
    }
    pub fn manual_role(&self) -> Option<&str> {
        match &self.authority {
            Authority::Manual(identity) => Some(&identity.state.role),
            Authority::Unclaimed(_) => None,
        }
    }
    pub fn stopped_worker_instance(&self) -> Option<&str> {
        match &self.authority {
            Authority::Manual(identity) => identity.state.stopped_worker.get().map(String::as_str),
            Authority::Unclaimed(_) => None,
        }
    }
    fn check_for(&self, service: &GuardService, pending_pause: bool) -> ControlResult<()> {
        if !Weak::ptr_eq(&self.service, &service.weak) || service.closed.load(Ordering::Acquire) {
            return Err(ControlError::Denied);
        }
        let owners = service.owners.read().map_err(|_| ControlError::Denied)?;
        let owner = owners.get(&(
            self.target.session_id.clone(),
            self.target.stream_id.clone(),
        ));
        match &self.authority {
            Authority::Unclaimed(revoked) => {
                if revoked.load(Ordering::Acquire) || owner.is_some() {
                    return Err(ControlError::Denied);
                }
            }
            Authority::Manual(identity) => {
                let owner = owner.ok_or(ControlError::Denied)?;
                if owner.stopped
                    || owner.returning
                    || owner.project_id != identity.project
                    || owner.token != identity.token
                    || owner.epoch != identity.epoch
                    || owner
                        .expires
                        .is_none_or(|expiry| service.clock.monotonic() >= expiry)
                    || owner
                        .manual
                        .as_ref()
                        .is_none_or(|state| !Arc::ptr_eq(state, &identity.state))
                {
                    return Err(ControlError::Denied);
                }
                if !pending_pause
                    && identity.state.role == "analyzer"
                    && (!identity.state.pause_confirmed.load(Ordering::Acquire)
                        || identity.state.stopped_worker.get().is_none())
                {
                    return Err(ControlError::Denied);
                }
            }
        }
        if service.closed.load(Ordering::Acquire) {
            return Err(ControlError::Denied);
        }
        Ok(())
    }
    pub fn check(&self) -> ControlResult<()> {
        let service = self.service.upgrade().ok_or(ControlError::Denied)?;
        let result = self.check_for(&service, false);
        if result.is_err() {
            service.control_changed.notify_waiters();
        }
        result
    }
    fn manual<'a>(
        &'a self,
        service: &GuardService,
        pending_pause: bool,
    ) -> ControlResult<&'a ManualIdentity> {
        self.check_for(service, pending_pause)?;
        match &self.authority {
            Authority::Manual(identity) => Ok(identity),
            Authority::Unclaimed(_) => Err(ControlError::Denied),
        }
    }
    fn check_runtime(&self, service: &GuardService, runtime: &ProjectRuntime) -> crate::Result<()> {
        let Authority::Manual(identity) = &self.authority else {
            return Err(ControlError::Denied.into());
        };
        let manual = runtime
            .manual
            .get(&self.target.stream_id)
            .ok_or(ControlError::Denied)?;
        let epoch = if manual.role == "receiver" {
            runtime.epoch
        } else {
            runtime.analyzer_epoch
        };
        if runtime.stopped
            || runtime.sid != self.target.session_id
            || epoch != identity.epoch
            || manual.token != identity.token
            || manual.returning
            || service.clock.monotonic() >= manual.expires
            || !Arc::ptr_eq(&manual.control, &identity.state)
        {
            return Err(ControlError::Denied.into());
        }
        Ok(())
    }
    fn expiry(&self, service: &GuardService) -> Option<Duration> {
        if matches!(&self.authority, Authority::Unclaimed(_)) {
            return None;
        }
        service
            .owners
            .read()
            .ok()?
            .get(&(
                self.target.session_id.clone(),
                self.target.stream_id.clone(),
            ))?
            .expires
    }
    /// Observe the same grant. Renewal only adjusts its cached lease deadline;
    /// this future never captures new permission or retains the service across wait.
    pub async fn revoked(&self) {
        loop {
            let Some(service) = self.service.upgrade() else {
                return;
            };
            let changed = service.control_changed.clone();
            let notified = changed.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            if self.check_for(&service, false).is_err() {
                return;
            }
            let remaining = self
                .expiry(&service)
                .map(|expiry| expiry.saturating_sub(service.clock.monotonic()));
            drop(service);
            if let Some(remaining) = remaining {
                tokio::select! { _ = notified => {}, _ = tokio::time::sleep(remaining) => {} }
            } else {
                notified.await;
            }
        }
    }
}
impl GuardService {
    fn capture_control(
        &self,
        target: &ControlTarget,
        control_owner: Option<&str>,
        pending_pause: bool,
    ) -> ControlResult<InstrumentControlGrant> {
        if self.closed.load(Ordering::Acquire) {
            return Err(ControlError::Denied);
        }
        let owners = self.owners.read().map_err(|_| ControlError::Denied)?;
        let authority = if let Some(owner) =
            owners.get(&(target.session_id.clone(), target.stream_id.clone()))
        {
            let token = control_owner.ok_or(ControlError::Denied)?;
            let state = owner.manual.as_ref().ok_or(ControlError::Denied)?;
            if owner.stopped
                || owner.returning
                || owner.token != token
                || owner
                    .expires
                    .is_none_or(|expiry| self.clock.monotonic() >= expiry)
            {
                return Err(ControlError::Denied);
            }
            if !pending_pause
                && state.role == "analyzer"
                && (!state.pause_confirmed.load(Ordering::Acquire)
                    || state.stopped_worker.get().is_none())
            {
                return Err(ControlError::Denied);
            }
            Authority::Manual(ManualIdentity {
                project: owner.project_id.clone(),
                token: token.into(),
                epoch: owner.epoch,
                state: state.clone(),
            })
        } else {
            if control_owner.is_some() {
                return Err(ControlError::Denied);
            }
            Authority::Unclaimed(
                self.control_grants
                    .lock()
                    .map_err(|_| ControlError::Denied)?
                    .capture(target),
            )
        };
        if self.closed.load(Ordering::Acquire) {
            return Err(ControlError::Denied);
        }
        Ok(InstrumentControlGrant {
            service: self.weak.clone(),
            target: target.clone(),
            authority,
        })
    }
    pub fn capture_instrument_control(
        &self,
        target: &ControlTarget,
        control_owner: Option<&str>,
    ) -> ControlResult<InstrumentControlGrant> {
        self.capture_control(target, control_owner, false)
    }
    pub(crate) fn pending_manual_control(
        &self,
        target: &ControlTarget,
        token: &str,
    ) -> ControlResult<InstrumentControlGrant> {
        self.capture_control(target, Some(token), true)
    }
    fn record_stopped(
        &self,
        grant: &InstrumentControlGrant,
        worker: &WorkerStatus,
        allow_unknown: bool,
    ) -> ControlResult<()> {
        let identity = grant.manual(self, true)?;
        if worker.session_id != grant.target.session_id
            || worker.stream_id != grant.target.stream_id
            || worker.alive
            || worker.state != "stopped"
            || identity.state.bound_worker.get().is_some()
        {
            return Err(ControlError::Denied);
        }
        match &identity.state.original_worker {
            Some(original)
                if !original.is_empty() && worker.worker_instance.as_ref() == Some(original) => {}
            None if allow_unknown => {}
            _ => return Err(ControlError::Denied),
        }
        self.update_runtime(&identity.project, |runtime| {
            grant.check_runtime(self, runtime)?;
            if identity.state.bound_worker.get().is_some() {
                return Err(ControlError::Denied.into());
            }
            if let Some(original) = &identity.state.original_worker {
                if identity
                    .state
                    .stopped_worker
                    .get()
                    .is_some_and(|old| old != original)
                {
                    return Err(ControlError::Denied.into());
                }
                let _ = identity.state.stopped_worker.set(original.clone());
            }
            identity
                .state
                .pause_confirmed
                .store(true, Ordering::Release);
            Ok(())
        })
        .map_err(|_| ControlError::Denied)
    }
    pub fn confirm_manual_worker_stopped(
        &self,
        grant: &InstrumentControlGrant,
        worker: &WorkerStatus,
    ) -> ControlResult<()> {
        self.record_stopped(grant, worker, false)
    }
    pub(crate) fn confirm_manual_acquire_stop(
        &self,
        grant: &InstrumentControlGrant,
        worker: &WorkerStatus,
    ) -> ControlResult<()> {
        self.record_stopped(grant, worker, true)
    }
    pub fn bind_manual_worker(
        &self,
        grant: &InstrumentControlGrant,
        worker: &WorkerStatus,
    ) -> ControlResult<()> {
        let identity = grant.manual(self, false)?;
        let original = identity
            .state
            .stopped_worker
            .get()
            .ok_or(ControlError::Denied)?;
        let instance = worker
            .worker_instance
            .as_ref()
            .filter(|instance| !instance.is_empty())
            .ok_or(ControlError::Denied)?;
        if !identity.state.pause_confirmed.load(Ordering::Acquire)
            || instance == original
            || worker.session_id != grant.target.session_id
            || worker.stream_id != grant.target.stream_id
            || !worker.alive
            || worker.state != "running"
            || worker.current.get("restart_required") == Some(&json!(true))
        {
            return Err(ControlError::Denied);
        }
        self.update_runtime(&identity.project, |runtime| {
            grant.check_runtime(self, runtime)?;
            identity
                .state
                .bound_worker
                .set(instance.clone())
                .map_err(|_| ControlError::Denied)?;
            runtime
                .manual
                .get_mut(&grant.target.stream_id)
                .ok_or(ControlError::Denied)?
                .worker_instance = Some(instance.clone());
            Ok(())
        })
        .map_err(|_| ControlError::Denied)?;
        grant.check_for(self, false)
    }
    pub async fn confirm_instrument_coverage(
        &self,
        grant: &InstrumentControlGrant,
    ) -> crate::Result<()> {
        grant.check_for(self, false)?;
        let Authority::Manual(identity) = &grant.authority else {
            return Ok(());
        };
        if identity.state.role != "analyzer" {
            return Ok(());
        }
        let _life = tokio::select! {
            life = self.lock_project(&identity.project) => life?,
            _ = grant.revoked() => return Err(ControlError::Denied.into()),
        };
        grant.check_for(self, false)?;
        self.confirm_instrument_coverage_locked(grant).await
    }
    pub(crate) async fn confirm_instrument_coverage_locked(
        &self,
        grant: &InstrumentControlGrant,
    ) -> crate::Result<()> {
        let identity = grant.manual(self, true)?;
        if identity.state.role != "analyzer"
            || !identity.state.pause_confirmed.load(Ordering::Acquire)
        {
            return Err(ControlError::Denied.into());
        }
        let _tx = self.coordinator.lock().await;
        grant.check_for(self, true)?;
        let mut project = self.project(&identity.project).await?;
        grant.check_for(self, true)?;
        let current = self
            .runtime_snapshot(&identity.project)?
            .ok_or(ControlError::Denied)?;
        grant.check_runtime(self, &current)?;
        if current.search.is_some() {
            return Err(ControlError::Denied.into());
        }
        project["coverage"] = json!("paused");
        self.archive
            .put_monitoring("project", project.clone())
            .await?;
        self.update_runtime(&identity.project, |runtime| {
            grant.check_runtime(self, runtime)?;
            runtime.cached_project = project;
            Ok(())
        })?;
        grant.check_for(self, true)?;
        Ok(())
    }
    pub(crate) fn invalidate_unclaimed(&self, target: &(String, String)) -> crate::Result<()> {
        self.control_grants
            .lock()
            .map_err(|_| {
                crate::GuardError::Conflict("guard control grant cache lock failed".into())
            })?
            .claim(target);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn thousand_one_off_queries_retain_only_live_absence_cells() {
        let mut cache = GrantCache::default();
        let retained = cache.capture(&ControlTarget {
            session_id: "retained".into(),
            stream_id: "one".into(),
        });
        for index in 0..1000 {
            let one = cache.capture(&ControlTarget {
                session_id: format!("one-off-{index}"),
                stream_id: "other".into(),
            });
            assert!(!one.load(Ordering::Acquire));
            assert!(cache.0.len() <= 2);
        }
        assert!(!retained.load(Ordering::Acquire));
        cache.claim(&("retained".into(), "one".into()));
        assert!(retained.load(Ordering::Acquire));
        let next = cache.capture(&ControlTarget {
            session_id: "retained".into(),
            stream_id: "one".into(),
        });
        assert!(!next.load(Ordering::Acquire));
        assert!(retained.load(Ordering::Acquire));
    }
}

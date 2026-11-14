//! Production binding for the closed instrument registry. This module owns no
//! serial/audio backend: effects use Core's existing owners and canonical writer.
mod audio;
mod backend;
mod http;
mod numeric;
mod science;
mod setup;
#[cfg(test)]
mod tests;
pub(crate) use http::{dispatch, handles};

use crate::error::{ApiError, ApiResult};
use crate::{Core, CoreError, devices, security::AuthReceipt};
use ori_control::{ControlError, Coordinator, OwnerReceipt, Target};
use ori_guard::InstrumentControlGrant;
use std::{
    collections::BTreeMap,
    sync::{
        Arc, Mutex, OnceLock, Weak,
        atomic::{AtomicBool, Ordering},
    },
};
use tokio::sync::Notify;

type Result<T> = ori_control::Result<T>;
// 128 retained jobs +32 contexts +32 live lease owners +serial admission.
// Setup stores only its connection scope, not another registered admission.
const MAX_SCOPES: usize = 256;
const MAX_CONNECTIONS: usize = 32;
const MAX_SETUP: usize = 64;
const MAX_TARGET_CELLS: usize = 128;

struct TargetCell {
    generation: std::sync::atomic::AtomicU64,
    lock: Arc<tokio::sync::Mutex<()>>,
}
impl TargetCell {
    fn check(&self, generation: u64) -> devices::DeviceResult<()> {
        if self.generation.load(Ordering::Acquire) == generation {
            Ok(())
        } else {
            Err(devices::DeviceError::Denied)
        }
    }
}
#[cfg(test)]
struct BindBarrier {
    used: AtomicBool,
    entered: tokio::sync::Semaphore,
    release: tokio::sync::Semaphore,
    stop_requested: tokio::sync::Semaphore,
}

/// Original admitted authority. It never resolves a target to a new worker.
#[derive(Clone)]
struct Scope {
    device: devices::control::CapturedDevice,
    auth: AuthReceipt,
    grant: InstrumentControlGrant,
}
struct ScopeEntry {
    owner: ori_control::WeakOwnerReceipt,
    scope: Weak<Scope>,
}
struct ActiveOperation {
    target: Target,
    worker: String,
}
struct Authority {
    target: Target,
    scope: Arc<Scope>,
}
impl ori_control::OwnerAuthority for Authority {
    fn target(&self) -> &Target {
        &self.target
    }
    fn worker_instance(&self) -> &str {
        self.scope.device.worker_instance()
    }
    fn connection_id(&self) -> &str {
        self.scope.device.connection_id()
    }
    fn revalidate(&self) -> Result<()> {
        self.scope.check()
    }
}
impl Scope {
    fn check(&self) -> Result<()> {
        self.auth.revalidate().map_err(|_| ControlError::Denied)?;
        self.grant.check().map_err(|_| ControlError::Denied)?;
        self.device.revalidate()
    }
}
pub(crate) struct InstrumentControl {
    core: Weak<Core>,
    coordinator: Coordinator,
    scopes: Mutex<Vec<ScopeEntry>>,
    connections: Mutex<BTreeMap<Target, Arc<Scope>>>,
    target_cells: Mutex<BTreeMap<Target, Weak<TargetCell>>>,
    setups: Mutex<tokio::task::JoinSet<()>>,
    setup_failed: AtomicBool,
    #[cfg(test)]
    bind_barrier: Mutex<Option<Arc<BindBarrier>>>,
    active: Mutex<BTreeMap<String, ActiveOperation>>,
    /// Serializes admission and a target-close snapshot, never hardware I/O.
    admitting: tokio::sync::Mutex<()>,
    closed: AtomicBool,
    runtime: tokio::runtime::Handle,
    close: OnceLock<CloseHandle>,
}
struct CloseState {
    result: Mutex<Option<std::result::Result<(), String>>>,
    changed: Notify,
    remaining: Mutex<Option<tokio::task::JoinSet<()>>>,
}
#[derive(Clone)]
pub(crate) struct CloseHandle(Arc<CloseState>);
impl CloseHandle {
    pub(crate) async fn completed(&self) -> crate::Result<()> {
        loop {
            let changed = self.0.changed.notified();
            tokio::pin!(changed);
            changed.as_mut().enable();
            if let Some(result) = self
                .0
                .result
                .lock()
                .expect("control close state poisoned")
                .clone()
            {
                return result
                    .map_err(|_| CoreError::Invalid("instrument cleanup did not complete"));
            }
            changed.await;
        }
    }
}
pub(crate) async fn initialize(core: &Arc<Core>) -> crate::Result<()> {
    if core.archive.is_none() {
        return Ok(());
    }
    let weak = Arc::downgrade(core);
    core.devices
        .as_ref()
        .ok_or(CoreError::Invalid("instrument owner missing"))?
        .install_instrument_cleanup_barrier(Arc::new(move || {
            let weak = weak.clone();
            Box::pin(async move {
                let core = weak.upgrade().ok_or(devices::DeviceError::Closed)?;
                core.control_stop_barrier().await.map_err(|_| {
                    devices::DeviceError::Worker(
                        "Guard shutdown intent failed before original transport cleanup".into(),
                    )
                })
            })
        }))?;
    let coordinator = Coordinator::new(
        Arc::new(backend::Executor {
            core: Arc::downgrade(core),
        }),
        Arc::new(backend::Committer {
            core: Arc::downgrade(core),
        }),
        ori_control::ControlConfig::default(),
    )
    .map_err(|_| CoreError::Invalid("instrument coordinator initialization"))?;
    core.instrument_control
        .set(Arc::new(InstrumentControl {
            core: Arc::downgrade(core),
            coordinator,
            scopes: Mutex::new(Vec::new()),
            connections: Mutex::new(BTreeMap::new()),
            target_cells: Mutex::new(BTreeMap::new()),
            setups: Mutex::new(tokio::task::JoinSet::new()),
            setup_failed: AtomicBool::new(false),
            #[cfg(test)]
            bind_barrier: Mutex::new(None),
            active: Mutex::new(BTreeMap::new()),
            admitting: tokio::sync::Mutex::new(()),
            closed: AtomicBool::new(false),
            runtime: tokio::runtime::Handle::current(),
            close: OnceLock::new(),
        }))
        .map_err(|_| CoreError::Invalid("instrument coordinator already initialized"))?;
    Ok(())
}
impl InstrumentControl {
    fn target_cell(&self, target: &Target) -> Result<Arc<TargetCell>> {
        let mut cells = self.target_cells.lock().map_err(|_| ControlError::Closed)?;
        cells.retain(|_, cell| cell.strong_count() > 0);
        if let Some(cell) = cells.get(target).and_then(Weak::upgrade) {
            return Ok(cell);
        }
        if cells.len() >= MAX_TARGET_CELLS {
            return Err(ControlError::Conflict("instrument setup target capacity"));
        }
        let cell = Arc::new(TargetCell {
            generation: std::sync::atomic::AtomicU64::new(0),
            lock: Arc::new(tokio::sync::Mutex::new(())),
        });
        cells.insert(target.clone(), Arc::downgrade(&cell));
        Ok(cell)
    }
    fn spawn_setup<F>(
        &self,
        future: F,
    ) -> ApiResult<tokio::sync::oneshot::Receiver<ApiResult<serde_json::Value>>>
    where
        F: std::future::Future<Output = ApiResult<serde_json::Value>> + Send + 'static,
    {
        let mut setups = self
            .setups
            .lock()
            .map_err(|_| ApiError::unavailable("instrument_setup_state"))?;
        while let Some(result) = setups.try_join_next() {
            if result.is_err() {
                self.setup_failed.store(true, Ordering::Release);
            }
        }
        if self.closed.load(Ordering::Acquire) {
            return Err(ApiError::unavailable("instrument_closed"));
        }
        if setups.len() >= MAX_SETUP {
            return Err(ApiError::unavailable("instrument_setup_capacity"));
        }
        let (send, received) = tokio::sync::oneshot::channel();
        setups.spawn_on(
            async move {
                let result = future.await;
                let _ = send.send(result);
            },
            &self.runtime,
        );
        Ok(received)
    }
    #[cfg(test)]
    async fn before_bind(&self) {
        let barrier = self.bind_barrier.lock().unwrap().clone();
        if let Some(barrier) = barrier
            && !barrier.used.swap(true, Ordering::AcqRel)
        {
            barrier.entered.add_permits(1);
            barrier.release.acquire().await.unwrap().forget();
        }
    }
    fn core(&self) -> Result<Arc<Core>> {
        self.core.upgrade().ok_or(ControlError::Closed)
    }
    fn scope(&self, owner: &OwnerReceipt) -> Result<Arc<Scope>> {
        owner.revalidate()?;
        let scope = self
            .scopes
            .lock()
            .map_err(|_| ControlError::Closed)?
            .iter()
            .find(|entry| entry.owner.matches(owner))
            .and_then(|entry| entry.scope.upgrade())
            .ok_or(ControlError::Denied)?;
        if !scope
            .device
            .matches(owner.worker_instance(), owner.connection_id())
        {
            return Err(ControlError::Denied);
        }
        scope.check()?;
        Ok(scope)
    }
    fn remember(&self, target: Target, scope: Arc<Scope>) -> Result<OwnerReceipt> {
        self.remember_connection(target.clone(), scope.clone())?;
        let owner = OwnerReceipt::from_backend(Arc::new(Authority {
            target: target.clone(),
            scope: scope.clone(),
        }))?;
        let mut scopes = self.scopes.lock().map_err(|_| ControlError::Closed)?;
        scopes.retain(|entry| entry.owner.upgrade().is_some());
        if scopes.len() >= MAX_SCOPES {
            return Err(ControlError::Conflict(
                "instrument admission context capacity",
            ));
        }
        scopes.push(ScopeEntry {
            owner: owner.downgrade(),
            scope: Arc::downgrade(&scope),
        });
        Ok(owner)
    }
    fn remember_connection(&self, target: Target, scope: Arc<Scope>) -> Result<()> {
        let mut connections = self.connections.lock().map_err(|_| ControlError::Closed)?;
        connections.retain(|_, scope| scope.device.is_alive());
        if connections.len() >= MAX_CONNECTIONS && !connections.contains_key(&target) {
            return Err(ControlError::Conflict(
                "instrument connection context capacity",
            ));
        }
        connections.insert(target, scope);
        Ok(())
    }
    fn snapshot(&self, target: &Target) -> Result<ori_control::CachedInstrument> {
        let core = self.core()?;
        let original = self
            .scopes
            .lock()
            .map_err(|_| ControlError::Closed)?
            .iter()
            .rev()
            .filter_map(|entry| entry.owner.upgrade().zip(entry.scope.upgrade()))
            .find(|(owner, scope)| owner.target() == target && scope.check().is_ok());
        let (owner, scope) = if let Some((owner, scope)) = original {
            (Some(owner), Some(scope))
        } else {
            let scope = self
                .connections
                .lock()
                .map_err(|_| ControlError::Closed)?
                .get(target)
                .cloned();
            let owner = scope
                .as_ref()
                .filter(|scope| scope.check().is_ok())
                .map(|scope| scope.device.owner());
            (owner, scope)
        };
        let mut snapshot = core
            .devices
            .as_ref()
            .ok_or(ControlError::Unavailable("instrument_owner"))?
            .instrument_snapshot(
                target,
                owner,
                core.config.analysis_worker.is_some(),
                core.audio.is_some(),
            )?;
        // No claimed stream is advertised as ready without an explicit original
        // grant. Metadata does not capture or manufacture manual authority.
        if let Some(domain) = core.domains.get() {
            snapshot.guard_claimed = domain
                .guard
                .authorize_control(
                    &ori_guard::ControlTarget {
                        session_id: target.session_id.clone(),
                        stream_id: target.stream_id.clone(),
                    },
                    None,
                )
                .is_err();
        }
        if let Some(scope) = scope {
            snapshot.coverage_bridge_available = scope.check().is_ok();
        }
        Ok(snapshot)
    }
    fn capture(
        &self,
        target: &Target,
        auth: AuthReceipt,
        control_owner: Option<&str>,
    ) -> Result<Arc<Scope>> {
        if self.closed.load(Ordering::Acquire) {
            return Err(ControlError::Closed);
        }
        let core = self.core()?;
        auth.revalidate().map_err(|_| ControlError::Denied)?;
        let grant = core
            .domains
            .get()
            .ok_or(ControlError::Unavailable("guard_owner"))?
            .guard
            .capture_instrument_control(
                &ori_guard::ControlTarget {
                    session_id: target.session_id.clone(),
                    stream_id: target.stream_id.clone(),
                },
                control_owner,
            )
            .map_err(|_| ControlError::Denied)?;
        if grant.manual_role().is_some() && grant.expected_worker_instance().is_none() {
            return Err(ControlError::Unavailable("manual_worker_not_bound"));
        }
        let gate = gate(
            auth.clone(),
            grant.clone(),
            grant.expected_worker_instance().map(str::to_owned),
        );
        let device = core
            .devices
            .as_ref()
            .ok_or(ControlError::Unavailable("instrument_owner"))?
            .capture_instrument(
                &devices::Target::new(&target.session_id, &target.stream_id),
                gate,
            )?;
        let scope = Scope {
            device,
            auth,
            grant,
        };
        scope.check()?;
        Ok(Arc::new(scope))
    }
    async fn submit(
        &self,
        target: &Target,
        raw: &[u8],
        client: Option<&str>,
        auth: AuthReceipt,
        control_owner: Option<&str>,
    ) -> Result<serde_json::Value> {
        let _admitting = self.admitting.lock().await;
        if self.closed.load(Ordering::Acquire) {
            return Err(ControlError::Closed);
        }
        {
            let mut active = self.active.lock().map_err(|_| ControlError::Closed)?;
            active.retain(|id, active| {
                self.coordinator
                    .get(&active.target, id, None)
                    .is_ok_and(|v| !terminal(&v))
            });
        }
        let scope = self.capture(target, auth, control_owner)?;
        let owner = self.remember(target.clone(), scope.clone())?;
        let mut snapshot = self.snapshot(target)?;
        snapshot.owner = Some(owner);
        snapshot.guard_claimed = scope.grant.manual_role().is_some();
        snapshot.coverage_bridge_available = true;
        let value = self
            .coordinator
            .submit_admitted(snapshot, raw, client)
            .await?;
        let id = value["operation_id"]
            .as_str()
            .ok_or(ControlError::Persistence(
                "missing committed operation identity".into(),
            ))?;
        self.active
            .lock()
            .map_err(|_| ControlError::Closed)?
            .insert(
                id.to_owned(),
                ActiveOperation {
                    target: target.clone(),
                    worker: scope.device.worker_instance().into(),
                },
            );
        Ok(value)
    }
    /// Cancellation-safe: one retained close attempt, even from a standard thread.
    pub(crate) fn begin_shutdown(&self) -> CloseHandle {
        self.closed.store(true, Ordering::Release);
        self.close
            .get_or_init(|| {
                let handle = CloseHandle(Arc::new(CloseState {
                    result: Mutex::new(None),
                    changed: Notify::new(),
                    remaining: Mutex::new(None),
                }));
                let done = handle.clone();
                let coordinator = self.coordinator.clone();
                let core = self.core.clone();
                let mut setups =
                    std::mem::take(&mut *self.setups.lock().expect("setup task registry poisoned"));
                let timeout = core
                    .upgrade()
                    .map(|core| {
                        core.config
                            .devices
                            .connect_timeout
                            .saturating_add(core.config.devices.stop_timeout.saturating_mul(2))
                    })
                    .unwrap_or(std::time::Duration::from_secs(15));
                let prior_failure = self.setup_failed.load(Ordering::Acquire);
                self.runtime.spawn(async move {
                    let setup_close = async {
                        let mut failed = prior_failure;
                        let drained = tokio::time::timeout(timeout, async {
                            while let Some(result) = setups.join_next().await {
                                failed |= result.is_err();
                            }
                        })
                        .await;
                        if drained.is_err() {
                            *done
                                .0
                                .remaining
                                .lock()
                                .expect("setup close retention poisoned") = Some(setups);
                            return Err("owned setup tasks remain active".to_owned());
                        }
                        failed |= core
                            .upgrade()
                            .and_then(|core| {
                                core.instrument_control
                                    .get()
                                    .map(|service| service.setup_failed.load(Ordering::Acquire))
                            })
                            .unwrap_or(false);
                        if failed {
                            Err("owned setup cleanup was not confirmed".to_owned())
                        } else {
                            Ok(())
                        }
                    };
                    let (controlled, setups) = tokio::join!(coordinator.close(), setup_close);
                    let result = controlled
                        .map_err(|_| "instrument cleanup failed".to_owned())
                        .and(setups);
                    *done.0.result.lock().expect("control close state poisoned") = Some(result);
                    done.0.changed.notify_waiters();
                });
                handle
            })
            .clone()
    }
    async fn cancel_instance_jobs(
        &self,
        target: &Target,
        instance: &str,
    ) -> devices::DeviceResult<()> {
        let ids = self
            .active
            .lock()
            .map_err(|_| devices::DeviceError::Closed)?
            .iter()
            .filter(|(_, active)| &active.target == target && active.worker == instance)
            .map(|(id, _)| id.clone())
            .collect::<Vec<_>>();
        for id in ids {
            self.coordinator.cancel(target, &id).await.map_err(|_| {
                devices::DeviceError::Worker(
                    "Instrument terminal result or original cleanup is not confirmed".into(),
                )
            })?;
        }
        Ok(())
    }
    /// A user stop retains the selected ready worker before any await. Old
    /// cleanup must never invalidate or select a replacement connection.
    async fn close_retained(
        &self,
        handle: &devices::RetainedWorkerHandle,
    ) -> devices::DeviceResult<devices::WorkerStatus> {
        #[cfg(test)]
        if let Some(barrier) = self.bind_barrier.lock().unwrap().as_ref() {
            barrier.stop_requested.add_permits(1);
        }
        let target = Target {
            session_id: handle.target().session_id.clone(),
            stream_id: handle.target().stream_id.clone(),
        };
        let cell = self
            .target_cell(&target)
            .map_err(|_| devices::DeviceError::QueueFull)?;
        let _target_guard = cell.lock.clone().lock_owned().await;
        let core = self.core().map_err(|_| devices::DeviceError::Closed)?;
        let manager = core.devices.as_ref().ok_or(devices::DeviceError::Closed)?;
        if manager.instrument_instance(handle.target())?.as_deref()
            == Some(handle.worker_instance())
        {
            cell.generation
                .try_update(Ordering::AcqRel, Ordering::Acquire, |value| {
                    value.checked_add(1)
                })
                .map_err(|_| devices::DeviceError::Closed)?;
        }
        let stopped = manager.stop_retained_worker(handle).await?;
        self.cancel_instance_jobs(&target, handle.worker_instance())
            .await?;
        Ok(stopped)
    }
    /// Trusted Core/Guard lifecycle hook. Target exclusion covers ready/bind,
    /// but is never held while calling a Guard project-lifecycle callback.
    pub(crate) async fn close_target(&self, target: &devices::Target) -> devices::DeviceResult<()> {
        let target = Target {
            session_id: target.session_id.clone(),
            stream_id: target.stream_id.clone(),
        };
        let cell = self
            .target_cell(&target)
            .map_err(|_| devices::DeviceError::QueueFull)?;
        #[cfg(test)]
        if let Some(barrier) = self.bind_barrier.lock().unwrap().as_ref() {
            barrier.stop_requested.add_permits(1);
        }
        let _target_guard = cell.lock.clone().lock_owned().await;
        let core = self.core().map_err(|_| devices::DeviceError::Closed)?;
        let manager = core.devices.as_ref().ok_or(devices::DeviceError::Closed)?;
        cell.generation
            .try_update(Ordering::AcqRel, Ordering::Acquire, |value| {
                value.checked_add(1)
            })
            .map_err(|_| devices::DeviceError::Closed)?;
        manager.request_instrument_stop(&devices::Target::new(
            &target.session_id,
            &target.stream_id,
        ))?;
        let admission = self.admitting.lock().await;
        let instance = manager
            .instrument_instance(&devices::Target::new(&target.session_id, &target.stream_id))?;
        let scope = self
            .connections
            .lock()
            .map_err(|_| devices::DeviceError::Closed)?
            .get(&target)
            .filter(|scope| instance.as_deref() == Some(scope.device.worker_instance()))
            .cloned();
        drop(admission);
        if let Some(instance) = instance {
            self.cancel_instance_jobs(&target, &instance).await?;
        }
        if let Some(scope) = scope {
            manager
                .cancel_instrument(&scope.device.owner())
                .await
                .map_err(|_| {
                    devices::DeviceError::Worker(
                        "Original instrument cleanup is not confirmed".into(),
                    )
                })?;
        }
        Ok(())
    }
}
fn terminal(value: &serde_json::Value) -> bool {
    matches!(
        value["state"].as_str(),
        Some("succeeded" | "failed" | "cancelled" | "unknown")
    )
}
fn gate(
    auth: AuthReceipt,
    grant: InstrumentControlGrant,
    expected: Option<String>,
) -> devices::ExecutionGate {
    devices::ExecutionGate::new(
        move || {
            auth.revalidate()
                .map_err(|_| devices::DeviceError::Denied)?;
            grant.check().map_err(|_| devices::DeviceError::Denied)
        },
        expected,
    )
}

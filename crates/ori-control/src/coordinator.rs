use crate::artifact::new_id;
use crate::*;
use ori_contracts::to_json_vec;
use ori_instruments::{ExecutionCheck, InstrumentError};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, VecDeque},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering},
    },
    time::Duration,
};
use subtle::ConstantTimeEq;
use tokio::{
    sync::{Mutex as AsyncMutex, OwnedSemaphorePermit, Semaphore},
    task::JoinHandle,
    time::Instant,
};
use tokio_util::sync::CancellationToken;
const METADATA_RESERVE: usize = MAX_REQUEST_BYTES * 2;

#[derive(Clone, Debug)]
pub struct ControlConfig {
    pub max_normal_per_instrument: usize,
    pub max_instruments: usize,
    pub max_normal_global: usize,
    pub max_history: usize,
    pub max_idempotency: usize,
    pub max_blob_bytes: usize,
    pub max_inflight_blob_bytes: usize,
    pub max_retained_bytes: usize,
    pub close_timeout: Duration,
}
impl Default for ControlConfig {
    fn default() -> Self {
        Self {
            max_normal_per_instrument: 4,
            max_instruments: 32,
            max_normal_global: 64,
            max_history: 128,
            max_idempotency: 2048,
            max_blob_bytes: MAX_BLOB_BYTES,
            max_inflight_blob_bytes: 32 * 1024 * 1024,
            max_retained_bytes: 32 * 1024 * 1024,
            close_timeout: Duration::from_secs(5),
        }
    }
}
impl ControlConfig {
    fn validate(&self) -> Result<()> {
        if !(1..=4).contains(&self.max_normal_per_instrument)
            || !(1..=32).contains(&self.max_instruments)
            || !(1..=64).contains(&self.max_normal_global)
            || !(1..=128).contains(&self.max_history)
            || !(1..=2048).contains(&self.max_idempotency)
            || self.max_blob_bytes == 0
            || self.max_blob_bytes > MAX_BLOB_BYTES
            || self.max_inflight_blob_bytes < self.max_blob_bytes
            || self.max_inflight_blob_bytes > 32 * 1024 * 1024
            || self.max_retained_bytes < (self.max_instruments + 2) * METADATA_RESERVE
            || self.max_retained_bytes > 32 * 1024 * 1024
            || self.close_timeout.is_zero()
            || self.close_timeout > Duration::from_secs(30)
        {
            return Err(ControlError::Invalid(
                "invalid bounded control configuration",
            ));
        }
        Ok(())
    }
}
struct Lease {
    epoch: u64,
    digest: Option<[u8; 32]>,
    expires: Instant,
    owner: Option<OwnerReceipt>,
    operation_id: Option<String>,
}
struct Context {
    normal: Arc<AsyncMutex<()>>,
    cancellation: AsyncMutex<()>,
    normal_active: AtomicUsize,
    off_active: AtomicBool,
    status: Mutex<Value>,
    lease: Mutex<Lease>,
    identity: (String, String),
    owner: OwnerReceipt,
    guard_claimed: bool,
    coverage_bridge: bool,
    science: bool,
    audio: bool,
    supported: std::collections::BTreeSet<OperationKind>,
    preview_epoch: AtomicU64,
}
struct LeaseAdmission {
    epoch: u64,
    digest: [u8; 32],
}
struct IoInFlight<'a>(&'a AtomicBool);
impl Drop for IoInFlight<'_> {
    fn drop(&mut self) {
        self.0.store(false, Ordering::SeqCst);
    }
}
struct Finished<'a>(&'a tokio::sync::Notify);
impl Drop for Finished<'_> {
    fn drop(&mut self) {
        self.0.notify_waiters();
    }
}
struct View {
    state: OperationState,
    result: Option<Value>,
    error: Option<String>,
}
struct Job {
    id: String,
    target: Target,
    request: Submission,
    owner: OwnerReceipt,
    context: Arc<Context>,
    guard_claimed: bool,
    coverage_bridge: bool,
    science: bool,
    audio: bool,
    created: Instant,
    created_at: f64,
    admission_revision: u64,
    preview_epoch: u64,
    view: Mutex<View>,
    cancel: CancellationToken,
    io_started: AtomicBool,
    io_inflight: AtomicBool,
    active: AtomicBool,
    lease: Option<LeaseAdmission>,
    lease_liveness: Option<u64>,
    retained: Mutex<Option<OwnedSemaphorePermit>>,
    handle: Mutex<Option<JoinHandle<()>>>,
    finished: tokio::sync::Notify,
}
type LedgerKey = (Target, Option<[u8; 32]>, String);
#[derive(Default)]
struct State {
    jobs: BTreeMap<String, Arc<Job>>,
    order: VecDeque<String>,
    ledger: BTreeMap<LedgerKey, ([u8; 32], String)>,
    contexts: BTreeMap<Target, Arc<Context>>,
}
struct Inner {
    executor: Arc<dyn ControlExecutor>,
    committer: Arc<dyn ControlCommitter>,
    config: ControlConfig,
    state: Mutex<State>,
    admission: AsyncMutex<()>,
    closing: AsyncMutex<()>,
    closed: AtomicBool,
    sticky: Mutex<Option<&'static str>>,
    normal_active: AtomicUsize,
    retained: Arc<Semaphore>,
    metadata_results: Arc<Semaphore>,
    off_results: Arc<Semaphore>,
    blobs: Arc<Semaphore>,
    started: Instant,
    stop: CancellationToken,
    maintenance: Mutex<Option<JoinHandle<()>>>,
    joined: Mutex<std::collections::BTreeSet<(Target, String, String)>>,
}
#[derive(Clone)]
pub struct Coordinator {
    inner: Arc<Inner>,
}

impl Coordinator {
    pub fn new(
        executor: Arc<dyn ControlExecutor>,
        committer: Arc<dyn ControlCommitter>,
        config: ControlConfig,
    ) -> Result<Self> {
        config.validate()?;
        tokio::runtime::Handle::try_current().map_err(|_| {
            ControlError::Invalid("control coordinator requires owned Tokio runtime")
        })?;
        let inner = Arc::new(Inner {
            executor,
            committer,
            retained: Arc::new(Semaphore::new(
                config.max_retained_bytes - (config.max_instruments + 1) * METADATA_RESERVE,
            )),
            metadata_results: Arc::new(Semaphore::new(METADATA_RESERVE)),
            off_results: Arc::new(Semaphore::new(config.max_instruments * METADATA_RESERVE)),
            blobs: Arc::new(Semaphore::new(config.max_inflight_blob_bytes)),
            config,
            state: Mutex::new(State::default()),
            admission: AsyncMutex::new(()),
            closing: AsyncMutex::new(()),
            closed: AtomicBool::new(false),
            sticky: Mutex::new(None),
            normal_active: AtomicUsize::new(0),
            started: Instant::now(),
            stop: CancellationToken::new(),
            maintenance: Mutex::new(None),
            joined: Mutex::new(std::collections::BTreeSet::new()),
        });
        let coordinator = Self { inner };
        let maintenance = coordinator.clone();
        *coordinator.inner.maintenance.lock().unwrap() = Some(tokio::spawn(async move {
            maintenance.maintain_leases().await;
        }));
        Ok(coordinator)
    }
    /// Local/oracle path captures once. Production HTTP must use submit_admitted.
    pub async fn submit(&self, target: &Target, raw: &[u8], client: Option<&str>) -> Result<Value> {
        let snapshot = self.inner.executor.snapshot(target)?;
        self.submit_admitted(snapshot, raw, client).await
    }
    /// Core wraps the snapshot's original backend owner with its ORIGINAL LAN
    /// credential revocation receipt. This method never recaptures authority.
    pub async fn submit_admitted(
        &self,
        snapshot: CachedInstrument,
        raw: &[u8],
        client: Option<&str>,
    ) -> Result<Value> {
        let request = parse_submission(raw, client)?;
        let _admission = self.inner.admission.lock().await;
        if self.inner.closed.load(Ordering::SeqCst) {
            return Err(ControlError::Closed);
        }
        if self.inner.sticky.lock().unwrap().is_some() {
            return Err(ControlError::Persistence(
                "control persistence is unavailable".into(),
            ));
        }
        if snapshot.family != InstrumentFamily::TinySa
            || !snapshot.supported_operations.contains(&request.kind)
        {
            return Err(ControlError::Unavailable(
                "operation_absent_from_selected_verified_profile",
            ));
        }
        let owner = snapshot
            .owner
            .clone()
            .ok_or(ControlError::Unavailable("owned_instrument_not_connected"))?;
        owner.revalidate()?;
        if self
            .inner
            .joined
            .lock()
            .unwrap()
            .contains(&owner_key(&owner))
        {
            return Err(ControlError::Unavailable("original_owned_worker_closed"));
        }
        if owner.target() != &snapshot.target {
            return Err(ControlError::Denied);
        }
        let key = request
            .request_id
            .as_ref()
            .map(|id| (snapshot.target.clone(), request.client_digest, id.clone()));
        let (job, context, key, renew, off) = {
            let mut state = self.inner.state.lock().unwrap();
            if let Some(key) = &key
                && let Some((digest, id)) = state.ledger.get(key)
            {
                if !bool::from(digest.ct_eq(&request.fingerprint)) {
                    return Err(ControlError::Conflict(
                        "request_id identifies another operation",
                    ));
                }
                let old = state.jobs.get(id).ok_or(ControlError::Conflict(
                    "history expired; mutation will not be replayed",
                ))?;
                return Ok(view(old, false, false));
            }
            if request.tracked()
                && key.is_some()
                && state.ledger.len() >= self.inner.config.max_idempotency
            {
                return Err(ControlError::Conflict(
                    "idempotency ledger full; no mutation accepted",
                ));
            }
            for id in state.order.iter().rev() {
                let old = &state.jobs[id];
                if old.target != snapshot.target {
                    continue;
                }
                let same = old.owner.worker_instance() == owner.worker_instance()
                    && old.owner.connection_id() == owner.connection_id();
                if same
                    && request.kind == OperationKind::Capture
                    && request.parameters.get("save") != Some(&Value::Bool(true))
                    && old.request.kind == OperationKind::Capture
                    && old.request.parameters.get("save") != Some(&Value::Bool(true))
                    && old.created.elapsed() < Duration::from_millis(500)
                    && old.admission_revision == snapshot.control_revision
                    && state.contexts.get(&snapshot.target).is_some_and(|ctx| {
                        ctx.preview_epoch.load(Ordering::SeqCst) == old.preview_epoch
                    })
                    && !old.cancel.is_cancelled()
                    && old.owner.revalidate().is_ok()
                {
                    return Ok(view(old, false, false));
                }
                if same
                    && request.kind == OperationKind::OutputOff
                    && old.request.kind == OperationKind::OutputOff
                    && !old.view.lock().unwrap().state.terminal()
                {
                    return Ok(view(old, false, false));
                }
            }
            let identity = (
                owner.worker_instance().to_owned(),
                owner.connection_id().to_owned(),
            );
            let context = if let Some(ctx) = state
                .contexts
                .get(&snapshot.target)
                .filter(|c| c.identity == identity)
            {
                ctx.clone()
            } else {
                if state.contexts.len() >= self.inner.config.max_instruments
                    && !state.contexts.contains_key(&snapshot.target)
                {
                    return Err(ControlError::Conflict("instrument context limit reached"));
                }
                if let Some(old) = state.contexts.get(&snapshot.target)
                    && (old.normal_active.load(Ordering::SeqCst) > 0
                        || old.off_active.load(Ordering::SeqCst))
                {
                    return Err(ControlError::Conflict(
                        "previous owned instrument operations are still active",
                    ));
                }
                let ctx = Arc::new(Context {
                    normal: Arc::new(AsyncMutex::new(())),
                    cancellation: AsyncMutex::new(()),
                    normal_active: AtomicUsize::new(0),
                    off_active: AtomicBool::new(false),
                    status: Mutex::new(snapshot.status.clone()),
                    lease: Mutex::new(Lease {
                        epoch: 0,
                        digest: None,
                        expires: Instant::now(),
                        owner: None,
                        operation_id: None,
                    }),
                    identity,
                    owner: owner.clone(),
                    guard_claimed: snapshot.guard_claimed,
                    coverage_bridge: snapshot.coverage_bridge_available,
                    science: snapshot.science_available,
                    audio: snapshot.audio_available,
                    supported: snapshot.supported_operations.clone(),
                    preview_epoch: AtomicU64::new(0),
                });
                state.contexts.insert(snapshot.target.clone(), ctx.clone());
                ctx
            };
            let renew = request.kind == OperationKind::LeaseRenew;
            let off = request.kind == OperationKind::OutputOff;
            if !renew
                && !off
                && (context.normal_active.load(Ordering::SeqCst)
                    >= self.inner.config.max_normal_per_instrument
                    || self.inner.normal_active.load(Ordering::SeqCst)
                        >= self.inner.config.max_normal_global)
            {
                return Err(ControlError::Conflict(
                    "instrument busy; command not queued",
                ));
            }
            if off && context.off_active.load(Ordering::SeqCst) {
                return Err(ControlError::Conflict("reserved output-off slot busy"));
            }
            while state.jobs.len() >= self.inner.config.max_history {
                if !evict_one(&mut state) {
                    return Err(ControlError::Conflict("job limit reached"));
                }
            }
            let reserve = request.bytes
                + if request.kind == OperationKind::Capture
                    && request.parameters.get("save") != Some(&Value::Bool(true))
                {
                    self.inner.config.max_blob_bytes.div_ceil(3) * 4 + 65536
                } else {
                    65536
                };
            let pool = if renew {
                self.inner.metadata_results.clone()
            } else if off {
                self.inner.off_results.clone()
            } else {
                self.inner.retained.clone()
            };
            let retained = loop {
                match pool.clone().try_acquire_many_owned(reserve as u32) {
                    Ok(p) => break p,
                    Err(_) => {
                        let evicted = if renew || off {
                            evict_kind(&mut state, job_kind(renew, off))
                        } else {
                            evict_one(&mut state)
                        };
                        if !evicted {
                            return Err(ControlError::Conflict(
                                "retained result/input byte budget full",
                            ));
                        }
                    }
                }
            };
            let lease = lease_at_admission(&context, &owner, request.lease_id.as_deref());
            let lease_liveness = {
                let lease = context.lease.lock().unwrap();
                (lease.digest.is_some() && lease.expires > Instant::now()).then_some(lease.epoch)
            };
            let job = Arc::new(Job {
                id: new_id(""),
                target: snapshot.target,
                request,
                owner,
                context: context.clone(),
                guard_claimed: snapshot.guard_claimed,
                coverage_bridge: snapshot.coverage_bridge_available,
                science: snapshot.science_available,
                audio: snapshot.audio_available,
                created: Instant::now(),
                created_at: self.inner.started.elapsed().as_secs_f64(),
                admission_revision: snapshot.control_revision,
                preview_epoch: context.preview_epoch.load(Ordering::SeqCst),
                view: Mutex::new(View {
                    state: OperationState::Queued,
                    result: None,
                    error: None,
                }),
                cancel: self.inner.stop.child_token(),
                io_started: AtomicBool::new(false),
                io_inflight: AtomicBool::new(false),
                active: AtomicBool::new(!renew),
                lease,
                lease_liveness,
                retained: Mutex::new(Some(retained)),
                handle: Mutex::new(None),
                finished: tokio::sync::Notify::new(),
            });
            // Admission is serialized, but no synchronous state lock spans commit.
            drop(state);
            (job, context, key, renew, off)
        };
        if let Err(error) = self
            .commit(&job, OperationState::Queued, None, None, None)
            .await
        {
            if matches!(
                job.request.kind,
                OperationKind::OutputOff
                    | OperationKind::LeaseRelease
                    | OperationKind::MonitorResume
            ) {
                let _ = self
                    .stop_owned(
                        &job.context,
                        &job.owner,
                        &job.id,
                        Instant::now() + self.inner.config.close_timeout,
                    )
                    .await;
            }
            return Err(error);
        }
        let submitted = {
            let mut state = self.inner.state.lock().unwrap();
            if job.request.tracked()
                && let Some(key) = key
            {
                state
                    .ledger
                    .insert(key, (job.request.fingerprint, job.id.clone()));
            }
            state.order.push_back(job.id.clone());
            state.jobs.insert(job.id.clone(), job.clone());
            if changes_preview(job.request.kind) {
                context.preview_epoch.fetch_add(1, Ordering::SeqCst);
            }
            // Only current contexts and bounded retained jobs can need a
            // previous completion proof. Reconnect history must not grow forever.
            let referenced = state
                .contexts
                .values()
                .map(|ctx| owner_key(&ctx.owner))
                .chain(state.jobs.values().map(|j| owner_key(&j.owner)))
                .collect::<std::collections::BTreeSet<_>>();
            self.inner
                .joined
                .lock()
                .unwrap()
                .retain(|key| referenced.contains(key));
            if !renew {
                if off {
                    context.off_active.store(true, Ordering::SeqCst);
                } else {
                    context.normal_active.fetch_add(1, Ordering::SeqCst);
                    self.inner.normal_active.fetch_add(1, Ordering::SeqCst);
                }
            }
            view(&job, false, false)
        };
        if renew {
            self.run_job(job).await;
        } else {
            let runner = self.clone();
            let work = job.clone();
            let handle = tokio::spawn(async move {
                runner.run_job(work.clone()).await;
                runner.release_slot(&work);
            });
            *job.handle.lock().unwrap() = Some(handle);
        }
        Ok(submitted)
    }
    pub fn get(&self, target: &Target, id: &str, client: Option<&str>) -> Result<Value> {
        let job = self.job(target, id)?;
        let reveal = client
            .map(|s| <[u8; 32]>::from(Sha256::digest(s.as_bytes())))
            .zip(job.request.client_digest)
            .is_some_and(|(a, b)| bool::from(a.ct_eq(&b)));
        let current = self
            .inner
            .executor
            .snapshot(target)
            .ok()
            .and_then(|s| s.owner)
            .is_some_and(|r| {
                r.worker_instance() == job.owner.worker_instance()
                    && r.connection_id() == job.owner.connection_id()
            });
        let current = current
            && !self
                .inner
                .joined
                .lock()
                .unwrap()
                .contains(&owner_key(&job.owner));
        let mut value = view(&job, true, reveal);
        value["context_current"] = json!(current);
        Ok(value)
    }
    pub fn list(&self, target: &Target) -> Result<Vec<Value>> {
        let state = self.inner.state.lock().unwrap();
        Ok(state
            .order
            .iter()
            .rev()
            .filter_map(|id| state.jobs.get(id))
            .filter(|j| &j.target == target)
            .take(20)
            .map(|j| view(j, false, false))
            .collect())
    }
    pub fn status(&self, target: &Target) -> Result<Value> {
        let snapshot = self.inner.executor.snapshot(target)?;
        let context = self
            .inner
            .state
            .lock()
            .unwrap()
            .contexts
            .get(target)
            .cloned();
        let mut status = snapshot.status;
        if let Some(ctx) = context {
            let same = snapshot.owner.is_some_and(|o| {
                (o.worker_instance(), o.connection_id())
                    == (ctx.identity.0.as_str(), ctx.identity.1.as_str())
            });
            if same {
                overlay_lease(&mut status, &ctx);
            }
        }
        if self.inner.sticky.lock().unwrap().is_some() {
            status["control_error"] =
                json!("operation persistence failed; control is closed to new work");
        }
        Ok(status)
    }
    pub fn capabilities(&self, target: &Target) -> Result<Value> {
        let snapshot = self.inner.executor.snapshot(target)?;
        if snapshot.family == InstrumentFamily::ArDv1 {
            return Ok(
                json!({"capabilities":{"profile":snapshot.status.get("profile"),"operations":{},"hardware_validation":"not_run","reason":"tinySA control operations unavailable; receiver uses selected verified Core registry"},"status":snapshot.status}),
            );
        }
        let mut caps = catalog();
        if let Some(ops) = caps["operations"].as_object_mut() {
            for (name, spec) in ops {
                spec["available"] = json!(OperationKind::parse(name).ok().is_some_and(|k| {
                    snapshot.supported_operations.contains(&k)
                        && (!matches!(k, OperationKind::Measurement) || snapshot.science_available)
                        && (!matches!(k, OperationKind::AudioInputs | OperationKind::AudioCapture)
                            || snapshot.audio_available)
                        && (!snapshot.guard_claimed
                            || snapshot.coverage_bridge_available
                            || !matches!(
                                k,
                                OperationKind::LeaseAcquire | OperationKind::MonitorResume
                            ))
                }));
            }
        }
        Ok(json!({"capabilities":caps,"status":self.status(target)?}))
    }
    pub async fn cancel(&self, target: &Target, id: &str) -> Result<Value> {
        let job = self.job(target, id)?;
        if job.view.lock().unwrap().state.terminal() {
            return self.get(target, id, None);
        }
        job.cancel.cancel();
        let deadline = Instant::now() + self.inner.config.close_timeout;
        if job.io_started.load(Ordering::SeqCst) {
            self.stop_owned(&job.context, &job.owner, &job.id, deadline)
                .await?;
        }
        tokio::time::timeout_at(deadline, async {
            loop {
                let notified = job.finished.notified();
                tokio::pin!(notified);
                notified.as_mut().enable();
                if job.view.lock().unwrap().state.terminal() {
                    return Ok(());
                }
                if self.inner.sticky.lock().unwrap().is_some() {
                    return Err(ControlError::Persistence(
                        "cancellation result was not committed".into(),
                    ));
                }
                notified.await;
            }
        })
        .await
        .map_err(|_| {
            ControlError::Indeterminate("cancellation result is not committed yet".into())
        })??;
        self.get(target, id, None)
    }
    pub async fn close(&self) -> Result<()> {
        let _close = self.inner.closing.lock().await;
        self.inner.closed.store(true, Ordering::SeqCst);
        self.inner.stop.cancel();
        // Wait for admission/queued commit before capturing the complete owned set.
        let deadline = Instant::now() + self.inner.config.close_timeout;
        let admission = match tokio::time::timeout_at(deadline, self.inner.admission.lock()).await {
            Ok(admission) => admission,
            Err(_) => {
                self.latch("admission commit remains active");
                return Err(ControlError::Persistence(
                    "admission commit remains active".into(),
                ));
            }
        };
        let (jobs, contexts) = {
            let state = self.inner.state.lock().unwrap();
            (
                state.jobs.values().cloned().collect::<Vec<_>>(),
                state.contexts.values().cloned().collect::<Vec<_>>(),
            )
        };
        for job in &jobs {
            job.cancel.cancel();
        }
        drop(admission);
        let mut owners = BTreeMap::new();
        for context in contexts {
            owners.insert(
                owner_key(&context.owner),
                (new_id("close-"), context.owner.clone(), context.clone()),
            );
        }
        for job in &jobs {
            if job.active.load(Ordering::SeqCst) && job.io_started.load(Ordering::SeqCst) {
                owners.insert(
                    owner_key(&job.owner),
                    (job.id.clone(), job.owner.clone(), job.context.clone()),
                );
            }
        }
        for (_, (operation_id, owner, context)) in owners {
            let _ = self
                .stop_owned(&context, &owner, &operation_id, deadline)
                .await;
        }
        for job in jobs {
            let handle = job.handle.lock().unwrap().take();
            if let Some(mut handle) = handle {
                match tokio::time::timeout_at(deadline, &mut handle).await {
                    Ok(Ok(())) => {}
                    Ok(Err(_)) => self.latch("owned control task failed"),
                    Err(_) => {
                        *job.handle.lock().unwrap() = Some(handle);
                        self.latch("owned control task remains active");
                    }
                }
            }
        }
        let maintenance = self.inner.maintenance.lock().unwrap().take();
        if let Some(mut handle) = maintenance {
            match tokio::time::timeout_at(deadline, &mut handle).await {
                Ok(Ok(())) => {}
                Ok(Err(_)) => self.latch("lease maintenance task failed"),
                Err(_) => {
                    *self.inner.maintenance.lock().unwrap() = Some(handle);
                    self.latch("lease maintenance remains active");
                }
            }
        }
        if let Some(error) = *self.inner.sticky.lock().unwrap() {
            return Err(ControlError::Persistence(error.into()));
        }
        Ok(())
    }
    fn job(&self, target: &Target, id: &str) -> Result<Arc<Job>> {
        self.inner
            .state
            .lock()
            .unwrap()
            .jobs
            .get(id)
            .filter(|j| &j.target == target)
            .cloned()
            .ok_or(ControlError::NotFound)
    }
    fn latch(&self, error: &'static str) {
        self.inner.sticky.lock().unwrap().get_or_insert(error);
    }
    async fn stop_owned(
        &self,
        context: &Context,
        owner: &OwnerReceipt,
        operation_id: &str,
        deadline: Instant,
    ) -> Result<()> {
        let result = tokio::time::timeout_at(deadline, async {
            let _cancel = context.cancellation.lock().await;
            let key = owner_key(owner);
            if self.inner.joined.lock().unwrap().contains(&key) {
                return Ok(());
            }
            // Trusted cleanup uses original ownership identity; it never obtains
            // another actor/current permission and must remain usable on revoke.
            let proof = self
                .inner
                .executor
                .cancel_and_join(CancellationRequest {
                    operation_id: operation_id.into(),
                    owner: owner.clone(),
                })
                .await?;
            if !proof.original_worker_joined || !proof.original_transport_dropped {
                return Err(ControlError::Indeterminate(
                    "original instrument I/O remains active".into(),
                ));
            }
            clear(context);
            self.inner.joined.lock().unwrap().insert(key);
            Ok(())
        })
        .await;
        match result {
            Ok(Ok(())) => Ok(()),
            _ => {
                self.latch("original instrument cancellation/transport drop not confirmed");
                Err(ControlError::Indeterminate(
                    "original instrument I/O remains active".into(),
                ))
            }
        }
    }
    fn release_slot(&self, job: &Job) {
        if job.active.swap(false, Ordering::SeqCst) {
            if job.request.kind == OperationKind::OutputOff {
                job.context.off_active.store(false, Ordering::SeqCst);
            } else {
                job.context.normal_active.fetch_sub(1, Ordering::SeqCst);
                self.inner.normal_active.fetch_sub(1, Ordering::SeqCst);
            }
        }
    }
    async fn run_job(&self, job: Arc<Job>) {
        let _finished = Finished(&job.finished);
        let _serial = if !matches!(
            job.request.kind,
            OperationKind::OutputOff | OperationKind::LeaseRenew
        ) {
            tokio::select! {guard=job.context.normal.clone().lock_owned()=>Some(guard),_=job.cancel.cancelled()=>None}
        } else {
            None
        };
        if job.cancel.is_cancelled() {
            let _ = self.finish(&job, Err(ControlError::Closed)).await;
            return;
        }
        if let Err(error) = self
            .commit(&job, OperationState::Running, None, None, None)
            .await
        {
            self.latch("running operation event was not committed");
            let _ = error;
            let _ = self
                .stop_owned(
                    &job.context,
                    &job.owner,
                    &job.id,
                    Instant::now() + self.inner.config.close_timeout,
                )
                .await;
            return;
        }
        job.view.lock().unwrap().state = OperationState::Running;
        let result = self.execute_job(&job).await;
        if self.finish(&job, result).await.is_err() {
            let _ = self
                .stop_owned(
                    &job.context,
                    &job.owner,
                    &job.id,
                    Instant::now() + self.inner.config.close_timeout,
                )
                .await;
        }
    }
    async fn execute_job(&self, job: &Job) -> Result<crate::result::Prepared> {
        job.owner.revalidate()?;
        let operation = job.request.operation()?;
        let expected = if matches!(operation, Operation::LeaseRenew) {
            None
        } else {
            job.request.expected_revision()?
        };
        if matches!(operation, Operation::Measurement { .. }) && !job.science {
            return Err(ControlError::Unavailable("scientific_worker"));
        }
        if matches!(
            operation,
            Operation::AudioInputs | Operation::AudioCapture(_)
        ) && !job.audio
        {
            return Err(ControlError::Unavailable("selected_audio_input"));
        }
        let lab = job.context.status.lock().unwrap().get("mode") == Some(&json!("lab"));
        let lease_required = needs_lease(&operation, lab);
        if lease_required && job.lease.is_none() {
            // Report an absent original lab permission only while the original
            // owner is still valid. Revocation/cancellation keeps its distinct
            // authority failure, and the same per-I/O check remains below.
            job.owner.revalidate()?;
            if job.cancel.is_cancelled() {
                return Err(ControlError::Denied);
            }
            return Err(ControlError::LabLeaseRequired);
        }
        if lab
            && job.lease_liveness.is_none()
            && !matches!(
                operation,
                Operation::LeaseAcquire { .. } | Operation::OutputOff
            )
        {
            let lease = job.context.lease.lock().unwrap();
            if lease.digest.is_some() && lease.expires > Instant::now() {
                return Err(ControlError::Denied);
            }
        }
        let check = combined_check(job, lease_required);
        check().map_err(|_| ControlError::Denied)?;
        if matches!(operation, Operation::LeaseRenew) {
            let mut result = job.context.status.lock().unwrap().clone();
            overlay_lease(&mut result, &job.context);
            return Ok(crate::result::Prepared {
                status: result.clone(),
                result,
                artifact: None,
                inflight: None,
            });
        }
        if matches!(operation, Operation::LeaseAcquire { .. }) {
            let lease = job.context.lease.lock().unwrap();
            if lease.digest.is_some() && lease.expires > Instant::now() {
                return Err(ControlError::Conflict(
                    "instrument already has an active lab lease",
                ));
            }
        }
        let changes_coverage = matches!(
            operation,
            Operation::LeaseAcquire { .. } | Operation::MonitorResume
        );
        if changes_coverage && job.guard_claimed && !job.coverage_bridge {
            return Err(ControlError::Unavailable(
                "explicit_guard_manual_coverage_bridge",
            ));
        }
        if changes_coverage && job.coverage_bridge {
            let transition = if matches!(operation, Operation::MonitorResume) {
                CoverageTransition::ResumeRequested
            } else {
                CoverageTransition::PauseRequested
            };
            self.inner
                .executor
                .coverage(CoverageRequest {
                    owner: job.owner.clone(),
                    transition,
                    actual_status: None,
                })
                .await?;
            check().map_err(|_| ControlError::Denied)?;
        }
        let blob = if produces_blob(&operation) {
            Some(
                tokio::select! {p=self.inner.blobs.clone().acquire_many_owned(self.inner.config.max_blob_bytes as u32)=>p.map_err(|_|ControlError::Closed)?,_=job.cancel.cancelled()=>return Err(ControlError::Closed)},
            )
        } else {
            None
        };
        check().map_err(|_| ControlError::Denied)?;
        job.io_started.store(true, Ordering::SeqCst);
        job.io_inflight.store(true, Ordering::SeqCst);
        let _io = IoInFlight(&job.io_inflight);
        let mut outcome = self
            .inner
            .executor
            .execute(ExecutionRequest {
                operation_id: job.id.clone(),
                target: job.target.clone(),
                owner: job.owner.clone(),
                expected_revision: expected,
                operation: operation.clone(),
                check: check.clone(),
            })
            .await?;
        if outcome.execution.owner().target() != job.owner.target()
            || outcome.execution.owner().worker_instance() != job.owner.worker_instance()
            || outcome.execution.owner().connection_id() != job.owner.connection_id()
            || outcome.applied_revision < outcome.execution.control_revision()
        {
            return Err(ControlError::Indeterminate(
                "backend returned a different execution receipt".into(),
            ));
        }
        if changes_coverage && job.coverage_bridge {
            let transition = if matches!(operation, Operation::MonitorResume) {
                CoverageTransition::ResumeConfirmed
            } else {
                CoverageTransition::PauseConfirmed
            };
            let confirmation = self
                .inner
                .executor
                .coverage(CoverageRequest {
                    owner: job.owner.clone(),
                    transition,
                    actual_status: Some(outcome.status.clone()),
                })
                .await?;
            if !confirmation.confirmed {
                return Err(ControlError::Indeterminate(
                    "coverage transition is not confirmed".into(),
                ));
            }
        }
        overlay_lease(&mut outcome.status, &job.context);
        let mut prepared = crate::result::prepare(
            &operation,
            outcome,
            &self.inner.executor,
            check,
            self.inner.config.max_blob_bytes,
        )
        .await?;
        prepared.inflight = blob;
        Ok(prepared)
    }
    async fn finish(&self, job: &Job, result: Result<crate::result::Prepared>) -> Result<()> {
        let mut inflight = None;
        let mut pending_lease = None;
        let mut clear_lease = false;
        let mut renew = false;
        let mut artifact = None;
        let mut status = None;
        let (state, mut output, error) = match result {
            Ok(mut prepared) => {
                inflight = prepared.inflight.take();
                match job.request.kind {
                    OperationKind::LeaseAcquire => {
                        let secret = new_id("");
                        let digest = Sha256::digest(secret.as_bytes()).into();
                        prepared.status["lease_active"] = json!(true);
                        prepared.status["lease_seconds"] = json!(30);
                        prepared.result =
                            json!({"lease_id":secret,"expires_in":30,"status":prepared.status});
                        pending_lease = Some(digest);
                    }
                    OperationKind::LeaseRenew => {
                        renew = true;
                        prepared.result["lease_active"] = json!(true);
                        prepared.result["lease_seconds"] = json!(30);
                    }
                    OperationKind::LeaseRelease | OperationKind::MonitorResume => {
                        clear_lease = true;
                        prepared.status["lease_active"] = json!(false);
                        prepared.status["lease_seconds"] = json!(0);
                        prepared.result = prepared.status.clone();
                    }
                    _ => {}
                }
                if let Some(value) = prepared.artifact.take() {
                    if job.request.kind == OperationKind::Capture
                        && job.request.parameters.get("save") != Some(&Value::Bool(true))
                    {
                        prepared.result["preview"] = value.preview();
                    } else {
                        prepared.result["artifact"] = value.summary();
                        artifact = Some(value);
                    }
                }
                status = Some(prepared.status);
                (OperationState::Succeeded, Some(prepared.result), None)
            }
            Err(error) => {
                let state = if matches!(error, ControlError::Indeterminate(_)) {
                    OperationState::Unknown
                } else if job.cancel.is_cancelled() {
                    OperationState::Cancelled
                } else {
                    OperationState::Failed
                };
                (state, None, Some(public_error(&error)))
            }
        };
        let serialized = output
            .as_ref()
            .map(to_json_vec)
            .transpose()
            .map_err(|_| ControlError::Invalid("invalid finite result"))?;
        let actual = job.request.bytes + serialized.as_ref().map_or(0, Vec::len);
        let reserved = job
            .retained
            .lock()
            .unwrap()
            .as_ref()
            .map_or(0, OwnedSemaphorePermit::num_permits);
        if actual > reserved {
            self.latch("backend result exceeded its reserved byte budget");
            return Err(ControlError::Persistence(
                "result exceeds reserved byte budget".into(),
            ));
        }
        let summary = artifact.as_ref().map(ClosedArtifact::summary);
        let committed = match self
            .commit(job, state, error.as_deref(), status.as_ref(), artifact)
            .await
        {
            Ok(value) => value,
            Err(error) => {
                self.latch("terminal operation/artifact commit failed");
                return Err(error);
            }
        };
        if let Some(summary) = summary {
            if committed.artifact_summary.as_ref() != Some(&summary) {
                self.latch("committer did not acknowledge the same closed artifact");
                return Err(ControlError::Persistence(
                    "artifact acknowledgement mismatch".into(),
                ));
            }
            if let Some(value) = output.as_mut() {
                value["artifact"] = summary;
            }
        }
        if let Some(mut current) = status {
            if let Some(digest) = pending_lease {
                let mut lease = job.context.lease.lock().unwrap();
                if !job.cancel.is_cancelled()
                    && job.owner.revalidate().is_ok()
                    && !self
                        .inner
                        .joined
                        .lock()
                        .unwrap()
                        .contains(&owner_key(&job.owner))
                {
                    lease.epoch = lease.epoch.wrapping_add(1);
                    lease.digest = Some(digest);
                    // No generator is on during acquisition: the new ephemeral
                    // lease becomes usable only with its committed publication.
                    lease.expires = Instant::now() + Duration::from_secs(30);
                    lease.owner = Some(job.owner.clone());
                    lease.operation_id = Some(job.id.clone());
                } else if let Some(value) = output.as_mut() {
                    value["expires_in"] = json!(0);
                    value["status"]["lease_active"] = json!(false);
                    value["status"]["lease_seconds"] = json!(0);
                }
            }
            if clear_lease {
                clear(&job.context);
            }
            if renew {
                let mut lease = job.context.lease.lock().unwrap();
                if lease_matches(job, &lease) {
                    lease.expires = Instant::now() + Duration::from_secs(30);
                }
            }
            overlay_lease(&mut current, &job.context);
            let mut published = job.context.status.lock().unwrap();
            // Normal work and the reserved off lane can commit in either
            // order. This context belongs to one immutable connection/worker;
            // a late historical result must not regress its heartbeat cache.
            let older = current["revision"]
                .as_u64()
                .zip(published["revision"].as_u64())
                .is_some_and(|(incoming, previous)| incoming < previous);
            if !older {
                *published = current;
            }
        }
        if let Some(permit) = job.retained.lock().unwrap().as_mut() {
            let surplus = permit.num_permits().saturating_sub(actual);
            drop(permit.split(surplus));
        }
        let mut view = job.view.lock().unwrap();
        view.state = state;
        view.result = output;
        view.error = error;
        drop(inflight);
        Ok(())
    }
    async fn commit(
        &self,
        job: &Job,
        state: OperationState,
        error: Option<&str>,
        status: Option<&Value>,
        artifact: Option<ClosedArtifact>,
    ) -> Result<CommitOutcome> {
        let fields = json!({"state":state,"error":error,"created_at":job.created_at,"revision":status.and_then(|s|s.get("revision")),"artifact":artifact.as_ref().map(ClosedArtifact::summary)});
        let committed = self
            .inner
            .committer
            .commit(ControlCommit {
                event: OperationEvent {
                    event_type: match state {
                        OperationState::Queued => "instrument.operation.queued",
                        OperationState::Running => "instrument.operation.running",
                        OperationState::Succeeded => "instrument.operation.succeeded",
                        OperationState::Failed => "instrument.operation.failed",
                        OperationState::Cancelled => "instrument.operation.cancelled",
                        OperationState::Unknown => "instrument.operation.unknown",
                    },
                    target: job.target.clone(),
                    operation_id: job.id.clone(),
                    operation: job.request.kind,
                    state,
                    occurred_at: chrono::Utc::now(),
                    fields,
                },
                artifact,
            })
            .await?;
        if committed.event_sequence == 0 {
            return Err(ControlError::Persistence(
                "missing committed event sequence".into(),
            ));
        }
        Ok(committed)
    }
    async fn maintain_leases(&self) {
        loop {
            tokio::select! {_=self.inner.stop.cancelled()=>return,_=tokio::time::sleep(Duration::from_millis(100))=>{}}
            let contexts: Vec<_> = self
                .inner
                .state
                .lock()
                .unwrap()
                .contexts
                .iter()
                .map(|(t, c)| (t.clone(), c.clone()))
                .collect();
            for (target, context) in contexts {
                let expired = {
                    let mut lease = context.lease.lock().unwrap();
                    if lease.digest.is_none() {
                        None
                    } else {
                        let revoked = lease
                            .owner
                            .as_ref()
                            .is_none_or(|owner| owner.revalidate().is_err());
                        if lease.expires <= Instant::now() || revoked {
                            lease.digest = None;
                            lease.epoch = lease.epoch.wrapping_add(1);
                            lease.owner.take().map(|owner| {
                                (
                                    owner,
                                    lease
                                        .operation_id
                                        .take()
                                        .unwrap_or_else(|| new_id("lease-")),
                                    revoked,
                                )
                            })
                        } else {
                            None
                        }
                    }
                };
                let Some((owner, operation_id, revoked)) = expired else {
                    continue;
                };
                let active: Vec<_> = self
                    .inner
                    .state
                    .lock()
                    .unwrap()
                    .jobs
                    .values()
                    .filter(|job| {
                        Arc::ptr_eq(&job.context, &context)
                            && job.io_inflight.load(Ordering::SeqCst)
                    })
                    .cloned()
                    .collect();
                let forced_close = revoked || !active.is_empty();
                if forced_close {
                    for job in active {
                        job.cancel.cancel();
                    }
                    // Wake a silent pending exchange NOW; do not wait for its
                    // 90-second binary deadline or queue another I/O behind it.
                    let _ = self
                        .stop_owned(
                            &context,
                            &owner,
                            &operation_id,
                            Instant::now() + self.inner.config.close_timeout,
                        )
                        .await;
                }
                if context.coverage_bridge
                    && self
                        .inner
                        .executor
                        .coverage(CoverageRequest {
                            owner: owner.clone(),
                            transition: CoverageTransition::LeaseExpired,
                            actual_status: None,
                        })
                        .await
                        .is_err()
                {
                    self.latch("lease expiration coverage was not recorded");
                    let _ = self
                        .stop_owned(
                            &context,
                            &owner,
                            &operation_id,
                            Instant::now() + self.inner.config.close_timeout,
                        )
                        .await;
                    continue;
                }
                let event = ControlCommit {
                    event: OperationEvent {
                        event_type: if revoked {
                            "instrument.lease.revoked"
                        } else {
                            "instrument.lease.expired"
                        },
                        target: target.clone(),
                        operation_id: operation_id.clone(),
                        operation: OperationKind::LeaseRelease,
                        state: if forced_close {
                            OperationState::Unknown
                        } else {
                            OperationState::Queued
                        },
                        occurred_at: chrono::Utc::now(),
                        fields: json!({"reason":if revoked{"owner_revoked"}else{"expired"},"action":if forced_close{"closed_original"}else{"off_requested"},"automatic_resume":false}),
                    },
                    artifact: None,
                };
                if self.inner.committer.commit(event).await.is_err() {
                    self.latch("lease expiration event was not committed");
                    let _ = self
                        .stop_owned(
                            &context,
                            &owner,
                            &operation_id,
                            Instant::now() + self.inner.config.close_timeout,
                        )
                        .await;
                    continue;
                }
                if forced_close {
                    continue;
                }
                let snapshot = CachedInstrument {
                    target,
                    family: InstrumentFamily::TinySa,
                    status: context.status.lock().unwrap().clone(),
                    control_revision: 0,
                    owner: Some(owner.clone()),
                    supported_operations: context.supported.clone(),
                    guard_claimed: context.guard_claimed,
                    coverage_bridge_available: context.coverage_bridge,
                    science_available: context.science,
                    audio_available: context.audio,
                };
                // Only the ORIGINAL receipt, never a fresh worker or permission.
                if self
                    .submit_admitted(snapshot, br#"{"operation":"output.off"}"#, None)
                    .await
                    .is_err()
                {
                    let _ = self
                        .stop_owned(
                            &context,
                            &owner,
                            &operation_id,
                            Instant::now() + self.inner.config.close_timeout,
                        )
                        .await;
                }
            }
        }
    }
}
fn evict_one(state: &mut State) -> bool {
    let index = state.order.iter().position(|id| {
        let job = &state.jobs[id];
        job.view.lock().unwrap().state.terminal() && !job.active.load(Ordering::SeqCst)
    });
    if let Some(index) = index {
        let id = state.order.remove(index).unwrap();
        state.jobs.remove(&id);
        true
    } else {
        false
    }
}
fn job_kind(renew: bool, off: bool) -> OperationKind {
    if renew {
        OperationKind::LeaseRenew
    } else if off {
        OperationKind::OutputOff
    } else {
        unreachable!("reserved result pool")
    }
}
fn evict_kind(state: &mut State, kind: OperationKind) -> bool {
    let index = state.order.iter().position(|id| {
        let job = &state.jobs[id];
        job.request.kind == kind
            && job.view.lock().unwrap().state.terminal()
            && !job.active.load(Ordering::SeqCst)
    });
    if let Some(index) = index {
        let id = state.order.remove(index).unwrap();
        state.jobs.remove(&id);
        true
    } else {
        false
    }
}
fn owner_key(owner: &OwnerReceipt) -> (Target, String, String) {
    (
        owner.target().clone(),
        owner.worker_instance().to_owned(),
        owner.connection_id().to_owned(),
    )
}
fn view(job: &Job, result: bool, reveal: bool) -> Value {
    let data = job.view.lock().unwrap();
    let mut value = json!({"operation_id":job.id,"session_id":job.target.session_id,"stream_id":job.target.stream_id,"operation":job.request.kind,"state":data.state,"error":data.error,"created_at":job.created_at});
    if result {
        let mut output = data.result.clone().unwrap_or(Value::Null);
        if !reveal && let Some(obj) = output.as_object_mut() {
            obj.remove("lease_id");
        }
        value["result"] = output;
    }
    value
}
fn public_error(error: &ControlError) -> String {
    match error {
        ControlError::Invalid(_)
        | ControlError::Conflict(_)
        | ControlError::Denied
        | ControlError::LabLeaseRequired
        | ControlError::Unavailable(_) => error.to_string(),
        ControlError::Indeterminate(_) => {
            "Instrument state unknown; stop/start with a new owned connection required".into()
        }
        ControlError::Closed => "Instrument operation cancelled".into(),
        _ => "Instrument operation failed".into(),
    }
}
fn lease_at_admission(
    context: &Context,
    owner: &OwnerReceipt,
    token: Option<&str>,
) -> Option<LeaseAdmission> {
    let token = token?;
    let digest: [u8; 32] = Sha256::digest(token.as_bytes()).into();
    let lease = context.lease.lock().unwrap();
    let original = lease.owner.as_ref()?;
    if original.worker_instance() != owner.worker_instance()
        || original.connection_id() != owner.connection_id()
        || lease.expires <= Instant::now()
        || original.revalidate().is_err()
        || !lease.digest.is_some_and(|v| bool::from(v.ct_eq(&digest)))
    {
        return None;
    }
    Some(LeaseAdmission {
        epoch: lease.epoch,
        digest,
    })
}
fn lease_matches(job: &Job, lease: &Lease) -> bool {
    job.lease.as_ref().is_some_and(|original| {
        lease.epoch == original.epoch
            && lease.expires > Instant::now()
            && lease
                .owner
                .as_ref()
                .is_some_and(|owner| owner.revalidate().is_ok())
            && lease
                .digest
                .is_some_and(|v| bool::from(v.ct_eq(&original.digest)))
    })
}
fn combined_check(job: &Job, requires_lease: bool) -> ExecutionCheck {
    let owner = job.owner.clone();
    let cancel = job.cancel.clone();
    let ctx = job.context.clone();
    let lease = job.lease.as_ref().map(|l| (l.epoch, l.digest));
    // Read-only screen/SD actions need no secret token, but an already active
    // lab epoch still constrains their in-flight liveness. This grants no lab
    // mutation and never looks up or imports a Guard manual token.
    let liveness = if matches!(
        job.request.kind,
        OperationKind::OutputOff | OperationKind::LeaseAcquire
    ) {
        None
    } else {
        job.lease_liveness
    };
    Arc::new(move || {
        owner
            .revalidate()
            .map_err(|_| InstrumentError::PolicyDenied)?;
        if cancel.is_cancelled() {
            return Err(InstrumentError::PolicyDenied);
        }
        if requires_lease || liveness.is_some() {
            let current = ctx.lease.lock().unwrap();
            let live = current.expires > Instant::now()
                && current.digest.is_some()
                && current
                    .owner
                    .as_ref()
                    .is_some_and(|owner| owner.revalidate().is_ok());
            if !live || liveness.is_some_and(|epoch| current.epoch != epoch) {
                return Err(InstrumentError::PolicyDenied);
            }
            if requires_lease
                && !lease.is_some_and(|(epoch, digest)| {
                    current.epoch == epoch
                        && current.expires > Instant::now()
                        && current.digest.is_some_and(|v| bool::from(v.ct_eq(&digest)))
                })
            {
                return Err(InstrumentError::PolicyDenied);
            }
        }
        Ok(())
    })
}
fn needs_lease(operation: &Operation, lab: bool) -> bool {
    match operation {
        Operation::LeaseRenew
        | Operation::LeaseRelease
        | Operation::MonitorResume
        | Operation::Touch { .. }
        | Operation::Release
        | Operation::Keypad { .. }
        | Operation::MenuHome
        | Operation::SdDelete { .. }
        | Operation::PresetSave { .. }
        | Operation::PresetLoad { .. }
        | Operation::PresetImport { .. }
        | Operation::SelfTest { .. }
        | Operation::GeneratorSweep(_)
        | Operation::AudioCapture(_) => true,
        Operation::Setting(setting) => {
            lab || ori_instruments::setting_command(setting).is_ok_and(|c| c.requires_lab)
        }
        Operation::AcquireRaw(_)
        | Operation::AcquireZero(_)
        | Operation::Trace(_)
        | Operation::Marker(_) => lab,
        _ => false,
    }
}
fn produces_blob(operation: &Operation) -> bool {
    matches!(
        operation,
        Operation::Capture { .. }
            | Operation::SdRead { .. }
            | Operation::AcquireRaw(_)
            | Operation::AcquireZero(_)
            | Operation::Trace(TraceOperation::Read { .. })
            | Operation::PresetExport
            | Operation::Measurement { .. }
            | Operation::AudioCapture(_)
    )
}
fn changes_preview(kind: OperationKind) -> bool {
    matches!(
        kind,
        OperationKind::LeaseAcquire
            | OperationKind::LeaseRelease
            | OperationKind::MonitorResume
            | OperationKind::Setting
            | OperationKind::Touch
            | OperationKind::Release
            | OperationKind::Keypad
            | OperationKind::MenuHome
            | OperationKind::PresetSave
            | OperationKind::PresetLoad
            | OperationKind::PresetImport
            | OperationKind::Trace
            | OperationKind::Marker
            | OperationKind::SelfTest
            | OperationKind::GeneratorSweep
            | OperationKind::OutputOff
    )
}
fn overlay_lease(status: &mut Value, context: &Context) {
    let lease = context.lease.lock().unwrap();
    let active = lease.digest.is_some()
        && lease.expires > Instant::now()
        && lease
            .owner
            .as_ref()
            .is_some_and(|owner| owner.revalidate().is_ok());
    status["lease_active"] = json!(active);
    status["lease_seconds"] = if active {
        json!(
            lease
                .expires
                .saturating_duration_since(Instant::now())
                .as_secs_f64()
        )
    } else {
        json!(0)
    };
}
fn clear(context: &Context) {
    let mut lease = context.lease.lock().unwrap();
    lease.epoch = lease.epoch.wrapping_add(1);
    lease.digest = None;
    lease.owner = None;
    lease.operation_id = None;
}

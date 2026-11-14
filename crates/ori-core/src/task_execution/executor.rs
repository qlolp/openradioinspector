use super::{validation::*, *};
use futures_util::FutureExt;
use ori_tasks::{TaskContext, TaskEffects, TaskError, TaskFuture, TaskResult};
use serde_json::Value;
use std::{
    collections::BTreeMap,
    future::Future,
    panic::AssertUnwindSafe,
    sync::{
        Arc, Mutex, MutexGuard,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    time::Duration,
};
use tokio::{
    runtime::Handle,
    sync::{Notify, watch},
};

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}
fn cached<T>(call: impl FnOnce() -> TaskResult<T>) -> TaskResult<T> {
    std::panic::catch_unwind(AssertUnwindSafe(call)).unwrap_or_else(|_| Err(failed()))
}
async fn owned<T>(call: impl Future<Output = TaskResult<T>>) -> TaskResult<T> {
    AssertUnwindSafe(call)
        .catch_unwind()
        .await
        .unwrap_or_else(|_| Err(failed()))
}

struct Gate {
    original: Arc<dyn TaskAuthority>,
    revoked: AtomicBool,
    epoch: AtomicU64,
    changed: Notify,
}
impl Gate {
    fn revoke(&self) {
        self.revoked.store(true, Ordering::SeqCst);
        self.changed.notify_waiters();
    }
    fn bind(&self, epoch: u64) -> TaskResult<()> {
        self.revalidate()?;
        if epoch == 0 {
            return Err(denied());
        }
        let original = self
            .epoch
            .compare_exchange(0, epoch, Ordering::SeqCst, Ordering::SeqCst)
            .unwrap_or_else(|value| value);
        if original != 0 && original != epoch {
            return Err(denied());
        }
        self.revalidate()
    }
}
impl TaskAuthority for Gate {
    fn revalidate(&self) -> TaskResult<()> {
        if self.revoked.load(Ordering::SeqCst) {
            return Err(denied());
        }
        cached(|| self.original.revalidate())?;
        if self.revoked.load(Ordering::SeqCst) {
            return Err(denied());
        }
        Ok(())
    }
    fn revoked(&self) -> TaskFuture<'_, ()> {
        Box::pin(async move {
            loop {
                let changed = self.changed.notified();
                tokio::pin!(changed);
                changed.as_mut().enable();
                if self.revalidate().is_err() {
                    return Ok(());
                }
                tokio::select! {
                    _ = changed => {},
                    _ = owned(async { self.original.revoked().await }) => { self.revoke(); return Ok(()); }
                }
            }
        })
    }
}
struct Activation {
    gate: Arc<Gate>,
    targets: BTreeMap<String, Arc<dyn TaskTarget>>,
}
type Key = (u64, String);
type ResultWatch<T> = watch::Sender<Option<TaskResult<T>>>;
struct Entry {
    key: Key,
    original_task: Vec<u8>,
    binding: TaskBinding,
    gate: Arc<Gate>,
    target: Arc<dyn TaskTarget>,
    run: Arc<dyn TaskRun>,
    cancelled: AtomicBool,
    finish: AtomicBool,
    joined: AtomicBool,
    changed: Notify,
    started: ResultWatch<()>,
    completed: ResultWatch<TaskCompletion>,
}
impl Entry {
    fn check(&self) -> TaskResult<()> {
        self.gate.bind(self.key.0)?;
        if self.cancelled.load(Ordering::SeqCst) {
            return Err(denied());
        }
        cached(|| self.target.revalidate())
    }
    fn cancel(&self) {
        if !self.cancelled.swap(true, Ordering::SeqCst) {
            let _ = std::panic::catch_unwind(AssertUnwindSafe(|| self.run.cancel()));
        }
        self.changed.notify_waiters();
    }
    async fn interrupted(&self) {
        loop {
            let changed = self.changed.notified();
            tokio::pin!(changed);
            changed.as_mut().enable();
            if self.cancelled.load(Ordering::SeqCst) || self.gate.revalidate().is_err() {
                return;
            }
            tokio::select! { _ = changed => {}, _ = self.gate.revoked() => return }
        }
    }
    async fn finish_requested(&self) {
        loop {
            let changed = self.changed.notified();
            tokio::pin!(changed);
            changed.as_mut().enable();
            if self.finish.load(Ordering::SeqCst) {
                return;
            }
            changed.await;
        }
    }
    async fn effect<T>(&self, future: impl Future<Output = TaskResult<T>>) -> TaskResult<T> {
        let future = owned(future);
        tokio::pin!(future);
        tokio::select! {
            result = &mut future => result,
            _ = self.interrupted() => { self.cancel(); let _ = future.await; Err(denied()) }
        }
    }
}
struct Attempt {
    result: ResultWatch<()>,
    timeout: Duration,
}
/// Cloneable observation of one owned attempt; dropping it never cancels work.
#[derive(Clone)]
pub struct TaskCloseHandle {
    attempt: Arc<Attempt>,
}
impl TaskCloseHandle {
    /// Owner wait for the same attempt's actual result, without an observer
    /// deadline. Never retries cleanup or changes a published join failure.
    pub async fn wait_joined(&self) -> TaskResult<()> {
        receive(self.attempt.result.subscribe()).await
    }
    pub async fn completed(&self) -> TaskResult<()> {
        tokio::time::timeout(
            self.attempt.timeout,
            receive(self.attempt.result.subscribe()),
        )
        .await
        .map_err(|_| pending())?
    }
}
async fn receive<T: Clone>(mut receiver: watch::Receiver<Option<TaskResult<T>>>) -> TaskResult<T> {
    loop {
        if let Some(result) = receiver.borrow().clone() {
            return result;
        }
        receiver.changed().await.map_err(|_| pending())?;
    }
}
struct State {
    revision: u64,
    enabling: bool,
    activation: Option<Arc<Activation>>,
    entries: BTreeMap<Key, Arc<Entry>>,
    attempt: Option<Arc<Attempt>>,
    terminal: bool,
    sticky: Option<TaskError>,
}
struct Inner {
    config: TaskExecutionConfig,
    bindings: BTreeMap<String, TaskBinding>,
    runtime: Handle,
    state: Mutex<State>,
}
/// A facade over injected actual work. It is never a native backend itself.
pub struct TaskExecution {
    inner: Arc<Inner>,
}
impl TaskExecution {
    pub fn new(config: TaskExecutionConfig) -> TaskResult<Arc<Self>> {
        let bindings = bindings(&config)?;
        let runtime = Handle::try_current().map_err(|_| failed())?;
        Ok(Arc::new(Self {
            inner: Arc::new(Inner {
                config,
                bindings,
                runtime,
                state: Mutex::new(State {
                    revision: 0,
                    enabling: false,
                    activation: None,
                    entries: BTreeMap::new(),
                    attempt: None,
                    terminal: false,
                    sticky: None,
                }),
            }),
        }))
    }
    /// Existing full/public task DTOs supply name/status; no files are opened.
    pub fn admit_enable(
        &self,
        tasks: &[Value],
        original: Arc<dyn TaskAuthority>,
    ) -> TaskResult<()> {
        task_inventory(&self.inner.bindings, tasks)?;
        let revision = {
            let mut state = lock(&self.inner.state);
            if state.terminal
                || state.sticky.is_some()
                || state.enabling
                || state.activation.is_some()
                || !state.entries.is_empty()
                || state
                    .attempt
                    .as_ref()
                    .is_some_and(|attempt| !matches!(&*attempt.result.borrow(), Some(Ok(()))))
            {
                return Err(pending());
            }
            state.enabling = true;
            state.revision
        };
        let gate = Arc::new(Gate {
            original,
            revoked: AtomicBool::new(false),
            epoch: AtomicU64::new(0),
            changed: Notify::new(),
        });
        let captured = (|| {
            gate.revalidate()?;
            let mut targets = BTreeMap::new();
            for (name, binding) in &self.inner.bindings {
                let target = cached(|| self.inner.config.callbacks.capture(binding, gate.clone()))?;
                cached(|| target.revalidate())?;
                targets.insert(name.clone(), target);
            }
            gate.revalidate()?;
            Ok(targets)
        })();
        let mut state = lock(&self.inner.state);
        state.enabling = false;
        if revision != state.revision
            || state.terminal
            || state
                .attempt
                .as_ref()
                .is_some_and(|attempt| !matches!(&*attempt.result.borrow(), Some(Ok(()))))
        {
            gate.revoke();
            return Err(denied());
        }
        match captured {
            Ok(targets) => {
                state.attempt = None;
                state.activation = Some(Arc::new(Activation { gate, targets }));
                Ok(())
            }
            Err(error) => {
                gate.revoke();
                Err(error)
            }
        }
    }
    pub fn validate_task(&self, task: &Value) -> TaskResult<()> {
        task_binding(&self.inner.bindings, task).map(|_| ())
    }
    pub fn is_enabled(&self) -> bool {
        let activation = lock(&self.inner.state).activation.clone();
        activation.is_some_and(|activation| activation.gate.revalidate().is_ok())
    }
    pub fn revoke(&self) {
        self.inner.revoke();
    }
    pub fn begin_drain(&self) -> TaskCloseHandle {
        self.inner.begin_drain(false)
    }
    pub fn begin_shutdown(&self) -> TaskCloseHandle {
        self.inner.begin_drain(true)
    }
    pub async fn drain(&self) -> TaskResult<()> {
        self.begin_drain().completed().await
    }
    pub async fn close(&self) -> TaskResult<()> {
        self.begin_shutdown().completed().await
    }
}
impl Drop for TaskExecution {
    fn drop(&mut self) {
        self.inner.revoke();
    }
}
impl TaskEffects for TaskExecution {
    fn on_start(&self, context: TaskContext) -> TaskFuture<'_, ()> {
        // Capture admission synchronously: an unpolled old future cannot attach
        // to a later enable's authority when somebody finally awaits it.
        let admitted = self.inner.start(context);
        let inner = self.inner.clone();
        Box::pin(async move {
            let entry = admitted?;
            let result = receive(entry.started.subscribe()).await;
            if result.is_err() {
                inner.remove_joined(&entry);
            }
            result
        })
    }
    fn on_complete(&self, context: TaskContext) -> TaskFuture<'_, ()> {
        let admitted = self.inner.existing(context);
        let inner = self.inner.clone();
        Box::pin(async move {
            let entry = admitted?;
            if entry.check().is_err() {
                entry.cancel();
            }
            entry.finish.store(true, Ordering::SeqCst);
            entry.changed.notify_waiters();
            let result = receive(entry.completed.subscribe()).await;
            let result = result.and_then(|_| entry.check());
            inner.remove_joined(&entry);
            result
        })
    }
}
impl Inner {
    fn revoke(&self) {
        let (activation, entries) = {
            let mut state = lock(&self.state);
            match state.revision.checked_add(1) {
                Some(value) => state.revision = value,
                None => state.terminal = true,
            }
            (
                state.activation.clone(),
                state.entries.values().cloned().collect::<Vec<_>>(),
            )
        };
        if let Some(activation) = activation {
            activation.gate.revoke();
        }
        for entry in entries {
            entry.cancel();
        }
    }
    fn start(self: &Arc<Self>, context: TaskContext) -> TaskResult<Arc<Entry>> {
        let raw = context.task.as_dict();
        let original_task = ori_contracts::to_json_vec(&raw).map_err(|_| invalid())?;
        if original_task.len() > 16 * 1024 * 1024 {
            return Err(invalid());
        }
        let binding = task_binding(&self.bindings, &raw)?.clone();
        let activation = {
            let state = lock(&self.state);
            if state.terminal || state.sticky.is_some() || state.attempt.is_some() {
                return Err(pending());
            }
            state.activation.clone().ok_or_else(denied)?
        };
        activation.gate.bind(context.epoch)?;
        let target = activation
            .targets
            .get(&binding.task_name)
            .cloned()
            .ok_or_else(denied)?;
        cached(|| target.revalidate())?;
        let run = cached(|| target.prepare(context.clone()))?;
        let (started, _) = watch::channel(None);
        let (completed, _) = watch::channel(None);
        let entry = Arc::new(Entry {
            key: (context.epoch, binding.task_name.clone()),
            original_task,
            binding,
            gate: activation.gate.clone(),
            target,
            run,
            cancelled: AtomicBool::new(false),
            finish: AtomicBool::new(false),
            joined: AtomicBool::new(false),
            changed: Notify::new(),
            started,
            completed,
        });
        entry.check()?;
        {
            let mut state = lock(&self.state);
            if state.terminal
                || state.sticky.is_some()
                || state.attempt.is_some()
                || entry.gate.revoked.load(Ordering::SeqCst)
                || state
                    .activation
                    .as_ref()
                    .is_none_or(|current| !Arc::ptr_eq(current, &activation))
                || state.entries.len() >= self.config.max_active_runs
                || state.entries.contains_key(&entry.key)
                || state.entries.values().any(|old| {
                    old.binding.session_id == entry.binding.session_id
                        && old.binding.stream_id == entry.binding.stream_id
                })
            {
                return Err(pending());
            }
            state.entries.insert(entry.key.clone(), entry.clone());
        }
        let owned = self.clone();
        let admitted = entry.clone();
        self.runtime.spawn(async move {
            owned.run(admitted).await;
        });
        Ok(entry)
    }
    fn existing(&self, context: TaskContext) -> TaskResult<Arc<Entry>> {
        let raw = context.task.as_dict();
        let binding = task_binding(&self.bindings, &raw)?;
        let entry = lock(&self.state)
            .entries
            .get(&(context.epoch, binding.task_name.clone()))
            .cloned()
            .ok_or_else(denied)?;
        if ori_contracts::to_json_vec(&raw).map_err(|_| invalid())? != entry.original_task {
            return Err(denied());
        }
        Ok(entry)
    }
    fn remove_joined(&self, entry: &Arc<Entry>) {
        if !entry.joined.load(Ordering::SeqCst) {
            return;
        }
        let mut state = lock(&self.state);
        if state
            .entries
            .get(&entry.key)
            .is_some_and(|owned| Arc::ptr_eq(owned, entry))
        {
            state.entries.remove(&entry.key);
        }
    }
    async fn run(self: Arc<Self>, entry: Arc<Entry>) {
        let start = match entry.check() {
            Ok(()) => entry
                .effect(async { entry.run.start().await })
                .await
                .and_then(|()| entry.check()),
            Err(error) => Err(error),
        };
        let mut outcome = match start {
            Ok(()) => {
                entry.started.send_replace(Some(Ok(())));
                tokio::select! { _ = entry.finish_requested() => {}, _ = entry.interrupted() => entry.cancel() }
                match entry.check() {
                    Ok(()) => entry
                        .effect(async { entry.run.finish().await })
                        .await
                        .and_then(|output| completion(&entry.binding, &output).map(|()| output)),
                    Err(error) => Err(error),
                }
            }
            Err(error) => Err(error),
        };
        if outcome.is_err() {
            entry.cancel();
        }
        let join = owned(async { entry.run.join().await });
        tokio::pin!(join);
        let joined = tokio::select! {
            result = &mut join => result,
            _ = entry.interrupted() => { entry.cancel(); join.await }
        };
        if joined.is_ok() {
            entry.joined.store(true, Ordering::SeqCst);
            if outcome.is_ok() {
                outcome = entry.check().and(outcome);
            }
        } else {
            lock(&self.state).sticky = Some(pending());
            outcome = Err(pending());
        }
        if entry.started.borrow().is_none() {
            entry.started.send_replace(Some(Err(outcome
                .as_ref()
                .err()
                .cloned()
                .unwrap_or_else(failed))));
        }
        entry.completed.send_replace(Some(outcome));
    }
    fn begin_drain(self: &Arc<Self>, terminal: bool) -> TaskCloseHandle {
        self.revoke();
        let (attempt, entries) = {
            let mut state = lock(&self.state);
            state.terminal |= terminal;
            if let Some(attempt) = &state.attempt {
                return TaskCloseHandle {
                    attempt: attempt.clone(),
                };
            }
            let (result, _) = watch::channel(None);
            let attempt = Arc::new(Attempt {
                result,
                timeout: self.config.close_timeout,
            });
            state.attempt = Some(attempt.clone());
            (attempt, state.entries.values().cloned().collect::<Vec<_>>())
        };
        let owned = self.clone();
        let completion = attempt.clone();
        self.runtime.spawn(async move {
            let mut clean = true;
            for entry in &entries {
                let _ = receive(entry.completed.subscribe()).await;
                clean &= entry.joined.load(Ordering::SeqCst);
            }
            let mut state = lock(&owned.state);
            if clean && state.sticky.is_none() {
                state.entries.clear();
                state.activation = None;
                completion.result.send_replace(Some(Ok(())));
            } else {
                state.sticky = Some(pending());
                completion.result.send_replace(Some(Err(pending())));
            }
        });
        TaskCloseHandle { attempt }
    }
}
impl Drop for Inner {
    fn drop(&mut self) {
        // A failed join is not permission to release original run/Guard leases.
        // Pending lifecycle tasks hold Inner themselves. Completed failures keep
        // their owned run references until process exit if the facade vanishes.
        let state = self
            .state
            .get_mut()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        for (_, entry) in std::mem::take(&mut state.entries) {
            if !entry.joined.load(Ordering::SeqCst) {
                std::mem::forget(entry);
            }
        }
    }
}

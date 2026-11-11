//! The supervisor owns every dispatched task until JOIN. Device/science delays
//! never hold the supervisor mailbox or another session's dispatch slot.
use crate::{
    AutomationConfig, AutomationEffects, AutomationScope, PolicyKind, policy,
    state::{self, Check, Clock, Context, Entry, Monotonic},
};
use chrono::Utc;
use ori_archive::{Archive, EventDraft, Mutation};
use ori_runtime::worker::Cancellation;
use ori_session::{SessionResult, SessionService, TuneCompletionCheck};
use serde_json::{Value, json};
use std::{
    collections::{HashMap, HashSet},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};
use tokio::{
    sync::{Notify, mpsc, oneshot},
    task::{Id, JoinSet},
};

type Reply<T> = oneshot::Sender<SessionResult<T>>;
type Key = (String, PolicyKind);
enum Command {
    Configure(
        PolicyKind,
        String,
        Value,
        Option<Arc<dyn TuneCompletionCheck>>,
        Reply<Value>,
    ),
    Status(PolicyKind, String, Reply<Value>),
    Run(PolicyKind, String, Reply<Value>),
    Start(Reply<()>),
}
struct Completion {
    closing: Arc<AtomicBool>,
    finished: Mutex<Option<SessionResult<()>>>,
    wake: Notify,
    done: Notify,
}
#[derive(Clone)]
pub struct AutomationService {
    sender: mpsc::Sender<Command>,
    completion: Arc<Completion>,
}
struct Running {
    key: Key,
    epoch: u64,
    cancel: Cancellation,
    reply: Option<Reply<Value>>,
    scheduled: bool,
}
struct RunningSetup {
    original: Arc<dyn TuneCompletionCheck>,
    execution: Arc<dyn TuneCompletionCheck>,
}
impl TuneCompletionCheck for RunningSetup {
    fn revalidate(&self) -> SessionResult<()> {
        self.original.revalidate()?;
        self.execution.revalidate()
    }
}
struct Supervisor {
    archive: Archive,
    sessions: Arc<SessionService>,
    effects: Arc<dyn AutomationEffects>,
    config: AutomationConfig,
    clock: Clock,
    monotonic: Monotonic,
    receiver: mpsc::Receiver<Command>,
    completion: Arc<Completion>,
    entries: HashMap<Key, Arc<Entry>>,
    tasks: JoinSet<SessionResult<Value>>,
    running: HashMap<Id, Running>,
    started: HashSet<PolicyKind>,
}
fn identifier(id: &str) -> SessionResult<()> {
    if id.is_empty() || id.len() > 1024 || id.chars().any(char::is_control) {
        return Err(ori_session::SessionError::invalid("Некорректный ID сеанса"));
    }
    Ok(())
}
fn empty(kind: PolicyKind) -> Value {
    if kind == PolicyKind::Detection {
        json!([])
    } else {
        Value::Null
    }
}
impl AutomationService {
    /// Normalize a closed policy patch before an external binding stages its
    /// original authority. This is pure and uses the same validation as apply.
    pub fn validated_policy_patch(
        kind: PolicyKind,
        payload: Value,
        session: &Value,
    ) -> SessionResult<Value> {
        policy::validate_shape(kind, &payload)?;
        if serde_json::to_vec(&payload).map_or(true, |value| value.len() > 16384) {
            return Err(ori_session::SessionError::invalid(
                "Слишком большая политика",
            ));
        }
        policy::patch(kind, payload, session)
    }
    pub fn new(
        archive: Archive,
        sessions: Arc<SessionService>,
        effects: Arc<dyn AutomationEffects>,
        config: AutomationConfig,
    ) -> SessionResult<Self> {
        let origin = Instant::now();
        Self::with_sources(
            archive,
            sessions,
            effects,
            config,
            Arc::new(Utc::now),
            Arc::new(move || origin.elapsed().as_secs_f64()),
        )
    }
    pub fn with_sources(
        archive: Archive,
        sessions: Arc<SessionService>,
        effects: Arc<dyn AutomationEffects>,
        config: AutomationConfig,
        clock: Clock,
        monotonic: Monotonic,
    ) -> SessionResult<Self> {
        if !(1..=128).contains(&config.max_sessions)
            || config.max_in_flight == 0
            || config.max_in_flight > config.max_sessions * 3
            || !(1000..=100000).contains(&config.max_recent)
        {
            return Err(ori_session::SessionError::invalid(
                "Некорректные лимиты автоматизации",
            ));
        }
        if tokio::runtime::Handle::try_current().is_err() {
            return Err(state::error(
                "automation_unavailable",
                "Требуется исполнитель async-службы",
            ));
        }
        let (sender, receiver) = mpsc::channel(16);
        let completion = Arc::new(Completion {
            closing: Arc::new(AtomicBool::new(false)),
            finished: Mutex::new(None),
            wake: Notify::new(),
            done: Notify::new(),
        });
        let supervisor = Supervisor {
            archive,
            sessions,
            effects,
            config,
            clock,
            monotonic,
            receiver,
            completion: completion.clone(),
            entries: HashMap::new(),
            tasks: JoinSet::new(),
            running: HashMap::new(),
            started: HashSet::new(),
        };
        tokio::spawn(supervisor.run());
        Ok(Self { sender, completion })
    }
    fn send(&self, command: Command) -> SessionResult<()> {
        if self.completion.closing.load(Ordering::SeqCst) {
            return Err(state::closed());
        }
        self.sender.try_send(command).map_err(|error| match error {
            mpsc::error::TrySendError::Full(_) => {
                state::error("automation_busy", "Очередь автоматизации занята")
            }
            mpsc::error::TrySendError::Closed(_) => state::closed(),
        })
    }
    pub async fn configure(
        &self,
        kind: PolicyKind,
        session: &str,
        payload: Value,
    ) -> SessionResult<Value> {
        self.configure_admitted(kind, session, payload, None).await
    }
    async fn configure_admitted(
        &self,
        kind: PolicyKind,
        session: &str,
        payload: Value,
        original: Option<Arc<dyn TuneCompletionCheck>>,
    ) -> SessionResult<Value> {
        if let Some(original) = &original {
            original.revalidate()?;
        }
        identifier(session)?;
        policy::validate_shape(kind, &payload)?;
        if serde_json::to_vec(&payload).map_or(true, |v| v.len() > 16384) {
            return Err(ori_session::SessionError::invalid(
                "Слишком большая политика",
            ));
        }
        let (tx, rx) = oneshot::channel();
        self.send(Command::Configure(
            kind,
            session.into(),
            payload,
            original,
            tx,
        ))?;
        rx.await.map_err(|_| state::closed())?
    }
    /// Preserve the originating request across queued/coordinator admission.
    pub async fn configure_with_check(
        &self,
        kind: PolicyKind,
        session: &str,
        payload: Value,
        original: Arc<dyn TuneCompletionCheck>,
    ) -> SessionResult<Value> {
        self.configure_admitted(kind, session, payload, Some(original))
            .await
    }
    pub async fn status(&self, kind: PolicyKind, session: &str) -> SessionResult<Value> {
        identifier(session)?;
        let (tx, rx) = oneshot::channel();
        self.send(Command::Status(kind, session.into(), tx))?;
        rx.await.map_err(|_| state::closed())?
    }
    /// Deterministic one-pass trigger; dropping this waiter does not detach work.
    pub async fn run_once(&self, kind: PolicyKind, session: &str) -> SessionResult<Value> {
        identifier(session)?;
        let (tx, rx) = oneshot::channel();
        self.send(Command::Run(kind, session.into(), tx))?;
        rx.await.map_err(|_| state::closed())?
    }
    pub async fn start(&self) -> SessionResult<()> {
        let (tx, rx) = oneshot::channel();
        self.send(Command::Start(tx))?;
        rx.await.map_err(|_| state::closed())?
    }
    /// Must be awaited before the archive/lease/executor is closed. A cancelled
    /// waiter does not cancel owned draining; callbacks are never aborted.
    pub async fn close(&self) -> SessionResult<()> {
        self.completion.closing.store(true, Ordering::SeqCst);
        self.completion.wake.notify_one();
        loop {
            let notified = self.completion.done.notified();
            if let Some(result) = self.completion.finished.lock().unwrap().clone() {
                return result;
            }
            notified.await;
        }
    }
}
impl Supervisor {
    fn entry(&mut self, id: &str, kind: PolicyKind) -> SessionResult<Arc<Entry>> {
        let key = (id.to_owned(), kind);
        if let Some(entry) = self.entries.get(&key) {
            return Ok(entry.clone());
        }
        let sessions = self
            .entries
            .keys()
            .map(|(id, _)| id)
            .collect::<HashSet<_>>();
        if !sessions.contains(&id.to_owned()) && sessions.len() >= self.config.max_sessions {
            return Err(state::error(
                "automation_capacity",
                "Лимит runtime-сеансов автоматизации",
            ));
        }
        let entry = Entry::new(id.into(), kind);
        self.entries.insert(key, entry.clone());
        Ok(entry)
    }
    async fn configure(
        &mut self,
        kind: PolicyKind,
        id: &str,
        payload: Value,
        original: Option<Arc<dyn TuneCompletionCheck>>,
    ) -> SessionResult<Value> {
        let session = self.sessions.get_session(id).await?;
        let patch = AutomationService::validated_policy_patch(kind, payload, &session)?;
        let coordinator = self.archive.domain_coordinator();
        let _guard = coordinator.lock().await;
        if self.completion.closing.load(Ordering::SeqCst) {
            return Err(state::closed());
        }
        let existing = self.entries.get(&(id.into(), kind)).cloned();
        let previous = existing
            .as_ref()
            .map(|entry| entry.state.lock().unwrap().policy.clone())
            .unwrap_or_else(|| policy::defaults(kind));
        let mut policy = previous.clone();
        policy
            .as_object_mut()
            .unwrap()
            .extend(patch.as_object().unwrap().clone());
        policy::patch(kind, policy.clone(), &session)?;
        let enabled = policy["enabled"].as_bool().unwrap();
        let enabling = enabled && previous["enabled"] != true;
        let boundary = if kind == PolicyKind::Detection && enabling {
            Some(self.archive.sweep_ingestion_boundary(id).await?)
        } else {
            None
        };
        if self.completion.closing.load(Ordering::SeqCst) {
            return Err(state::closed());
        }
        if let Some(original) = &original {
            original.revalidate()?;
        }
        // Rejected queued admission cannot allocate a policy slot, cancel an
        // old run, advance its epoch, or publish an operator event.
        let entry = match existing {
            Some(entry) => entry,
            None => self.entry(id, kind)?,
        };
        for running in self
            .running
            .values()
            .filter(|running| running.key == (id.into(), kind))
        {
            running.cancel.cancel();
        }
        {
            let mut state = entry.state.lock().unwrap();
            entry.gate.epoch.fetch_add(1, Ordering::SeqCst);
            entry.gate.enabled.store(enabled, Ordering::SeqCst);
            state.policy = policy.clone();
            if kind != PolicyKind::Handoff || policy["enabled"] != previous["enabled"] {
                state.runtime["last_error"] = Value::Null;
                if kind == PolicyKind::Handoff {
                    state.runtime["consecutive_failures"] = json!(0);
                }
            }
            state.runtime["last_result"] = json!(if enabled { "enabled" } else { "disabled" });
            state.next = if enabled {
                (self.monotonic)()
            } else {
                f64::INFINITY
            };
            if let Some(boundary) = boundary {
                state.cursor = boundary;
                state.recent.clear();
            }
        }
        self.archive
            .apply(vec![Mutation::Event(EventDraft {
                event_type: format!("{}.policy_changed", kind.name()),
                occurred_at: state::stamp((self.clock)()),
                session_id: Some(id.into()),
                payload: json!({"policy":policy,"source":"operator_api"}),
            })])
            .await?;
        // The supervisor cannot dispatch until this cache-only handoff has
        // observed canonical ACK. It receives the same original Arc, never a
        // recaptured permission. An error here cannot roll back that commit.
        self.effects.policy_committed(
            AutomationScope {
                session_id: id.into(),
                kind,
                stream_id: policy[if kind == PolicyKind::Detection {
                    "stream_id"
                } else {
                    "receiver_stream_id"
                }]
                .as_str()
                .map(str::to_owned),
            },
            original,
        )?;
        if enabled && self.config.start_background {
            self.started.insert(kind);
        }
        Ok(entry.status(self.started.contains(&kind)))
    }
    fn dispatch(&mut self, entry: Arc<Entry>, reply: Option<Reply<Value>>, scheduled: bool) {
        let key = (entry.id.clone(), entry.kind);
        let result = (|| -> SessionResult<Option<Context>> {
            if !entry.gate.enabled.load(Ordering::SeqCst) {
                return Ok(None);
            }
            if self.running.len() >= self.config.max_in_flight
                || self.running.values().any(|running| running.key == key)
            {
                return Err(state::error(
                    "automation_busy",
                    "Политика уже выполняет независимый проход",
                ));
            }
            let policy = entry.state.lock().unwrap().policy.clone();
            let original = self.effects.capture(AutomationScope {
                session_id: entry.id.clone(),
                kind: entry.kind,
                stream_id: policy[if entry.kind == PolicyKind::Detection {
                    "stream_id"
                } else {
                    "receiver_stream_id"
                }]
                .as_str()
                .map(str::to_owned),
            })?;
            let check = Arc::new(Check {
                gate: entry.gate.clone(),
                epoch: entry.gate.epoch.load(Ordering::SeqCst),
                closing: self.completion.closing.clone(),
                cancel: Cancellation::default(),
                original,
            });
            check.revalidate()?;
            {
                let mut state = entry.state.lock().unwrap();
                state.runtime["last_run_at"] = json!(state::stamp((self.clock)()));
                state.next = (self.monotonic)() + policy["interval_seconds"].as_f64().unwrap();
            }
            Ok(Some(Context {
                archive: self.archive.clone(),
                sessions: self.sessions.clone(),
                effects: self.effects.clone(),
                entry: entry.clone(),
                policy,
                check,
                clock: self.clock.clone(),
                monotonic: self.monotonic.clone(),
                max_recent: self.config.max_recent,
            }))
        })();
        match result {
            Ok(Some(context)) => {
                let cancel = context.check.cancel.clone();
                let epoch = context.check.epoch;
                let handle = self.tasks.spawn(async move { run_policy(context).await });
                self.running.insert(
                    handle.id(),
                    Running {
                        key,
                        epoch,
                        cancel,
                        reply,
                        scheduled,
                    },
                );
            }
            Ok(None) => {
                if let Some(reply) = reply {
                    let _ = reply.send(Ok(empty(entry.kind)));
                }
            }
            Err(error) => {
                if scheduled {
                    self.record_error(&entry, &error);
                }
                if let Some(reply) = reply {
                    let _ = reply.send(Err(error));
                }
            }
        }
    }
    fn record_error(&self, entry: &Entry, error: &ori_session::SessionError) {
        let mut state = entry.state.lock().unwrap();
        state.runtime["last_result"] = json!("scheduler_error");
        state.runtime["last_error"] = json!(error.message);
        state.next = (self.monotonic)() + 1.0;
    }
    async fn command(&mut self, command: Command) {
        match command {
            Command::Configure(kind, id, payload, original, reply) => {
                let result = self.configure(kind, &id, payload, original).await;
                let _ = reply.send(result);
            }
            Command::Status(kind, id, reply) => {
                let result = self.sessions.get_session(&id).await.map(|_| {
                    self.entries
                        .get(&(id.clone(), kind))
                        .cloned()
                        .unwrap_or_else(|| Entry::new(id, kind))
                        .status(self.started.contains(&kind))
                });
                let _ = reply.send(result);
            }
            Command::Run(kind, id, reply) => match self.entry(&id, kind) {
                Ok(entry) => self.dispatch(entry, Some(reply), false),
                Err(error) => {
                    let _ = reply.send(Err(error));
                }
            },
            Command::Start(reply) => {
                self.started.extend([
                    PolicyKind::Detection,
                    PolicyKind::Handoff,
                    PolicyKind::Completion,
                ]);
                let _ = reply.send(Ok(()));
            }
        }
    }
    fn due(&mut self) {
        let now = (self.monotonic)();
        let mut entries = self
            .entries
            .values()
            .filter(|entry| {
                self.started.contains(&entry.kind)
                    && entry.gate.enabled.load(Ordering::SeqCst)
                    && !self
                        .running
                        .values()
                        .any(|r| r.key == (entry.id.clone(), entry.kind))
            })
            .filter_map(|entry| {
                let next = entry.state.lock().unwrap().next;
                (next <= now).then_some((next, entry.clone()))
            })
            .collect::<Vec<_>>();
        entries.sort_by(|a, b| {
            a.0.total_cmp(&b.0)
                .then(a.1.id.as_bytes().cmp(b.1.id.as_bytes()))
                .then(a.1.kind.cmp(&b.1.kind))
        });
        for (_, entry) in entries {
            if self.running.len() >= self.config.max_in_flight {
                break;
            }
            self.dispatch(entry, None, true);
        }
    }
    fn joined(&mut self, result: Result<(Id, SessionResult<Value>), tokio::task::JoinError>) {
        let (id, result) = match result {
            Ok(value) => value,
            Err(error) => (
                error.id(),
                Err(state::error(
                    "automation_task_failed",
                    "Задача автоматизации завершилась с ошибкой",
                )),
            ),
        };
        if let Some(running) = self.running.remove(&id) {
            if running.scheduled
                && let Err(error) = &result
                && let Some(entry) = self.entries.get(&running.key)
                && entry.gate.epoch.load(Ordering::SeqCst) == running.epoch
                && entry.gate.enabled.load(Ordering::SeqCst)
            {
                self.record_error(entry, error);
            }
            if let Some(reply) = running.reply {
                let _ = reply.send(result);
            }
        }
    }
    async fn run(mut self) {
        let mut timer = tokio::time::interval(Duration::from_millis(100));
        let mut draining = false;
        loop {
            if self.completion.closing.load(Ordering::SeqCst) && !draining {
                draining = true;
                self.receiver.close();
                for entry in self.entries.values() {
                    entry.gate.enabled.store(false, Ordering::SeqCst);
                    entry.gate.epoch.fetch_add(1, Ordering::SeqCst);
                }
                for running in self.running.values() {
                    running.cancel.cancel();
                }
                while let Ok(command) = self.receiver.try_recv() {
                    reject(command);
                }
            }
            if draining && self.tasks.is_empty() {
                self.entries.clear();
                *self.completion.finished.lock().unwrap() = Some(Ok(()));
                self.completion.done.notify_waiters();
                return;
            }
            tokio::select! {
                _=self.completion.wake.notified()=>{},
                command=self.receiver.recv(),if !draining=>{
                    if let Some(command)=command{self.command(command).await;}else{self.completion.closing.store(true,Ordering::SeqCst);}
                },
                Some(result)=self.tasks.join_next_with_id(),if !self.tasks.is_empty()=>self.joined(result),
                _=timer.tick(),if !self.started.is_empty() && !draining=>self.due(),
            }
        }
    }
}
fn reject(command: Command) {
    match command {
        Command::Configure(_, _, _, _, reply)
        | Command::Status(_, _, reply)
        | Command::Run(_, _, reply) => {
            let _ = reply.send(Err(state::closed()));
        }
        Command::Start(reply) => {
            let _ = reply.send(Err(state::closed()));
        }
    }
}
async fn run_policy(mut context: Context) -> SessionResult<Value> {
    context.check.revalidate()?;
    let session = context.sessions.get_session(&context.entry.id).await?;
    if session["state"] != "running" {
        context.result("session_not_running")?;
        return Ok(empty(context.entry.kind));
    }
    let original: Arc<dyn TuneCompletionCheck> = context.check.clone();
    let execution = context.effects.setup_running(
        AutomationScope {
            session_id: context.entry.id.clone(),
            kind: context.entry.kind,
            stream_id: context.policy[if context.entry.kind == PolicyKind::Detection {
                "stream_id"
            } else {
                "receiver_stream_id"
            }]
            .as_str()
            .map(str::to_owned),
        },
        original.clone(),
    )?;
    context.check = Arc::new(Check {
        gate: context.check.gate.clone(),
        epoch: context.check.epoch,
        closing: context.check.closing.clone(),
        cancel: context.check.cancel.clone(),
        original: Arc::new(RunningSetup {
            original,
            execution,
        }),
    });
    context.check.revalidate()?;
    match context.entry.kind {
        PolicyKind::Detection => crate::detection::run(&context, &session).await,
        PolicyKind::Handoff => crate::handoff::run(&context).await,
        PolicyKind::Completion => crate::completion::run(&context).await,
    }
}

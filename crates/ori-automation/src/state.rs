use crate::{AutomationEffects, PolicyKind, policy};
use chrono::{DateTime, SecondsFormat, Utc};
use ori_archive::{Archive, EventDraft, Mutation};
use ori_runtime::worker::Cancellation;
use ori_session::{SessionError, SessionResult, SessionService, TuneCompletionCheck};
use serde_json::{Value, json};
use std::sync::{
    Arc, Mutex, MutexGuard,
    atomic::{AtomicBool, AtomicU64, Ordering},
};

#[derive(Clone)]
pub struct AutomationConfig {
    pub start_background: bool,
    pub max_sessions: usize,
    pub max_in_flight: usize,
    pub max_recent: usize,
}
impl Default for AutomationConfig {
    fn default() -> Self {
        Self {
            start_background: true,
            max_sessions: 64,
            max_in_flight: 16,
            max_recent: 5000,
        }
    }
}
pub(crate) fn error(code: &str, message: &str) -> SessionError {
    SessionError::new(503, code, message)
}
pub(crate) fn closed() -> SessionError {
    error("automation_closed", "Планировщик уже закрыт")
}
pub(crate) fn stamp(at: DateTime<Utc>) -> String {
    at.to_rfc3339_opts(
        if at.timestamp_subsec_micros() == 0 {
            SecondsFormat::Secs
        } else {
            SecondsFormat::Micros
        },
        true,
    )
}
pub(crate) type Clock = Arc<dyn Fn() -> DateTime<Utc> + Send + Sync>;
pub(crate) type Monotonic = Arc<dyn Fn() -> f64 + Send + Sync>;
pub(crate) struct Gate {
    pub epoch: AtomicU64,
    pub enabled: AtomicBool,
}
pub(crate) struct Recent {
    pub stream: String,
    pub frequency: u64,
    pub bandwidth: u64,
    pub expires: f64,
}
pub(crate) struct State {
    pub policy: Value,
    pub runtime: Value,
    pub cursor: u64,
    pub recent: Vec<Recent>,
    pub next: f64,
}
pub(crate) struct Entry {
    pub id: String,
    pub kind: PolicyKind,
    pub gate: Arc<Gate>,
    pub state: Mutex<State>,
}
impl Entry {
    pub fn new(id: String, kind: PolicyKind) -> Arc<Self> {
        let mut runtime = json!({"last_run_at":null,"last_result":"disabled","last_error":null});
        let extra = match kind {
            PolicyKind::Detection => {
                json!({"last_sweep_id":null,"sweeps_processed":0,"candidates_created":0,"duplicates_suppressed":0})
            }
            PolicyKind::Handoff => {
                json!({"last_investigation_id":null,"consecutive_failures":0,"successful_dispatches":0,"failed_dispatches":0})
            }
            PolicyKind::Completion => {
                json!({"last_investigation_id":null,"completed_investigations":0,"skipped_investigations":0})
            }
        };
        runtime
            .as_object_mut()
            .unwrap()
            .extend(extra.as_object().unwrap().clone());
        Arc::new(Self {
            id,
            kind,
            gate: Arc::new(Gate {
                epoch: AtomicU64::new(0),
                enabled: AtomicBool::new(false),
            }),
            state: Mutex::new(State {
                policy: policy::defaults(kind),
                runtime,
                cursor: 0,
                recent: Vec::new(),
                next: f64::INFINITY,
            }),
        })
    }
    pub fn status(&self, started: bool) -> Value {
        let state = self.state.lock().unwrap();
        let mut value = json!({"policy":state.policy,"runtime":state.runtime,"scheduler_alive":started,"policy_persistent":false,"restart_requires_opt_in":true});
        if self.kind == PolicyKind::Detection {
            value["processes_existing_archive"] = json!(false);
            value["automatically_enqueues_investigations"] = json!(false);
        }
        if self.kind == PolicyKind::Completion {
            value["automatic_fail_or_cancel"] = json!(false);
        }
        value
    }
}
pub(crate) struct Check {
    pub gate: Arc<Gate>,
    pub epoch: u64,
    pub closing: Arc<AtomicBool>,
    pub cancel: Cancellation,
    pub original: Arc<dyn TuneCompletionCheck>,
}
impl TuneCompletionCheck for Check {
    fn revalidate(&self) -> SessionResult<()> {
        if self.closing.load(Ordering::SeqCst)
            || self.cancel.is_cancelled()
            || !self.gate.enabled.load(Ordering::SeqCst)
            || self.gate.epoch.load(Ordering::SeqCst) != self.epoch
        {
            return Err(SessionError::new(
                409,
                "automation_policy_changed",
                "Политика или исходное разрешение изменились",
            ));
        }
        self.original.revalidate()
    }
}
pub(crate) struct Context {
    pub archive: Archive,
    pub sessions: Arc<SessionService>,
    pub effects: Arc<dyn AutomationEffects>,
    pub entry: Arc<Entry>,
    pub policy: Value,
    pub check: Arc<Check>,
    pub clock: Clock,
    pub monotonic: Monotonic,
    pub max_recent: usize,
}
impl Context {
    /// Policy swaps take this same state lock before changing the epoch, so a
    /// delayed task cannot roll a re-enabled detector's checkpoint backwards.
    pub fn current_state(&self) -> SessionResult<MutexGuard<'_, State>> {
        let state = self.entry.state.lock().unwrap();
        self.check.revalidate()?;
        Ok(state)
    }
    pub fn result(&self, value: &str) -> SessionResult<()> {
        self.current_state()?.runtime["last_result"] = json!(value);
        Ok(())
    }
    pub async fn publish(&self, kind: &str, payload: Value) -> SessionResult<()> {
        let coordinator = self.archive.domain_coordinator();
        let _guard = coordinator.lock().await;
        self.check.revalidate()?;
        self.archive
            .apply(vec![Mutation::Event(EventDraft {
                event_type: kind.into(),
                occurred_at: stamp((self.clock)()),
                session_id: Some(self.entry.id.clone()),
                payload,
            })])
            .await?;
        Ok(())
    }
    pub fn increment(&self, key: &str, amount: u64) -> SessionResult<()> {
        let mut state = self.current_state()?;
        let previous = state.runtime[key].as_u64().unwrap_or(0);
        state.runtime[key] = json!(previous.saturating_add(amount));
        Ok(())
    }
}

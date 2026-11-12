//! Default-off automation over Core's existing canonical and effect owners.
mod authority;
mod effects;
mod http;
mod science;
#[cfg(test)]
mod tests;

use crate::{Core, CoreError};
use authority::{Authority, Key, Pending, Registry};
pub(crate) use http::{get, handles, post};
use ori_automation::{AutomationConfig, AutomationService, PolicyKind};
use ori_session::{SessionError, SessionResult, TuneCompletionCheck};
use serde_json::Value;
use std::sync::{
    Arc, Mutex, OnceLock, Weak,
    atomic::{AtomicBool, Ordering},
};
use tokio::{sync::Notify, task::JoinSet};

const MAX_POLICY_KEYS: usize = 64 * 3;
const MAX_CONFIGURATIONS: usize = 16;

pub(crate) struct AutomationBinding {
    core: Weak<Core>,
    service: AutomationService,
    registry: Arc<Mutex<Registry>>,
    closed: Arc<AtomicBool>,
    configuring: tokio::sync::Mutex<()>,
    owned: Mutex<JoinSet<()>>,
    failed: AtomicBool,
    runtime: tokio::runtime::Handle,
    close: OnceLock<CloseHandle>,
}
struct CloseState {
    result: Mutex<Option<SessionResult<()>>>,
    changed: Notify,
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
                .expect("automation close state")
                .clone()
            {
                return result.map_err(CoreError::from);
            }
            changed.await;
        }
    }
}
pub(crate) async fn initialize(core: &Arc<Core>) -> crate::Result<()> {
    let Some(archive) = core.archive.as_ref() else {
        return Ok(());
    };
    let domains = core
        .domains
        .get()
        .ok_or(CoreError::Invalid("automation session owner missing"))?;
    let registry = Arc::new(Mutex::new(Registry::default()));
    let closed = Arc::new(AtomicBool::new(false));
    let effects = Arc::new(effects::Effects {
        core: Arc::downgrade(core),
        registry: registry.clone(),
        closed: closed.clone(),
    });
    // New service construction leaves every scheduler off. Only explicit
    // configuration starts the selected kind, preserving original Draft opt-in.
    let service = AutomationService::new(
        archive.clone(),
        domains.session.clone(),
        effects,
        AutomationConfig::default(),
    )?;
    core.automation
        .set(Arc::new(AutomationBinding {
            core: Arc::downgrade(core),
            service,
            registry,
            closed,
            configuring: tokio::sync::Mutex::new(()),
            owned: Mutex::new(JoinSet::new()),
            failed: AtomicBool::new(false),
            runtime: tokio::runtime::Handle::current(),
            close: OnceLock::new(),
        }))
        .map_err(|_| CoreError::Invalid("automation owner already initialized"))?;
    Ok(())
}
fn unavailable() -> SessionError {
    SessionError::new(
        503,
        "automation_unavailable",
        "Служба автоматизации недоступна",
    )
}
fn denied() -> SessionError {
    SessionError::new(
        409,
        "automation_policy_changed",
        "Политика или исходное разрешение изменились",
    )
}
impl AutomationBinding {
    fn check(&self) -> SessionResult<()> {
        if self.closed.load(Ordering::Acquire) {
            Err(unavailable())
        } else {
            Ok(())
        }
    }
    pub(crate) fn begin_shutdown(self: &Arc<Self>) -> CloseHandle {
        self.closed.store(true, Ordering::Release);
        if let Ok(registry) = self.registry.lock() {
            registry.revoke_all();
        }
        self.close
            .get_or_init(|| {
                let handle = CloseHandle(Arc::new(CloseState {
                    result: Mutex::new(None),
                    changed: Notify::new(),
                }));
                let done = handle.clone();
                let mut owned =
                    std::mem::take(&mut *self.owned.lock().expect("automation task registry"));
                let service = self.service.clone();
                let prior_failure = self.failed.load(Ordering::Acquire);
                self.runtime.spawn(async move {
                    let drain = async {
                        let mut failed = prior_failure;
                        while let Some(result) = owned.join_next().await {
                            failed |= result.is_err();
                        }
                        if failed { Err(unavailable()) } else { Ok(()) }
                    };
                    // Configuration and policy work retain their owners even when
                    // the HTTP waiter or this CloseHandle waiter disappears.
                    let (service, configurations) = tokio::join!(service.close(), drain);
                    *done.0.result.lock().expect("automation close result") =
                        Some(service.and(configurations));
                    done.0.changed.notify_waiters();
                });
                handle
            })
            .clone()
    }
    async fn configure(
        self: &Arc<Self>,
        kind: PolicyKind,
        session: String,
        payload: Value,
        auth: crate::security::AuthReceipt,
        control_owner: Option<String>,
    ) -> SessionResult<Value> {
        self.check()?;
        let (send, received) = tokio::sync::oneshot::channel();
        {
            let mut owned = self.owned.lock().map_err(|_| unavailable())?;
            while let Some(result) = owned.try_join_next() {
                if result.is_err() {
                    self.failed.store(true, Ordering::Release);
                }
            }
            self.check()?;
            if owned.len() >= MAX_CONFIGURATIONS {
                return Err(SessionError::new(
                    503,
                    "automation_busy",
                    "Очередь автоматизации занята",
                ));
            }
            let binding = self.clone();
            owned.spawn_on(
                async move {
                    let result = binding
                        .configure_owned(kind, &session, payload, auth, control_owner)
                        .await;
                    let _ = send.send(result);
                },
                &self.runtime,
            );
        }
        received.await.map_err(|_| unavailable())?
    }
    async fn configure_owned(
        &self,
        kind: PolicyKind,
        id: &str,
        payload: Value,
        auth: crate::security::AuthReceipt,
        control_owner: Option<String>,
    ) -> SessionResult<Value> {
        let _configuration = self.configuring.lock().await;
        self.check()?;
        authority::auth(&auth)?;
        let core = self.core.upgrade().ok_or_else(unavailable)?;
        let domains = core.domains.get().ok_or_else(unavailable)?;
        let session = domains.session.get_session(id).await?;
        authority::auth(&auth)?;
        let patch = AutomationService::validated_policy_patch(kind, payload, &session)?;
        let mut policy = self.service.status(kind, id).await?["policy"].clone();
        policy
            .as_object_mut()
            .ok_or_else(unavailable)?
            .extend(patch.as_object().expect("validated policy object").clone());
        // Validation remains in the library. In particular, a malformed update
        // cannot temporarily replace the active policy's original authority.
        AutomationService::validated_policy_patch(kind, policy.clone(), &session)?;
        let enrollment = core
            .archive
            .as_ref()
            .ok_or_else(unavailable)?
            .capture_session_lifecycle(id)
            .await
            .map_err(authority::archive)?;
        authority::auth(&auth)?;
        self.check()?;
        let authority = Authority::capture(
            &core,
            kind,
            id,
            &session,
            &policy,
            authority::Admission {
                auth,
                control_owner,
                closed: self.closed.clone(),
                enrollment,
            },
        )?;
        let key = Key::new(id, kind);
        let check: Arc<dyn TuneCompletionCheck> = authority.clone();
        {
            let mut registry = self.registry.lock().map_err(|_| unavailable())?;
            self.check()?;
            registry.prune();
            if registry.policies.len() >= MAX_POLICY_KEYS && !registry.policies.contains_key(&key) {
                return Err(SessionError::new(
                    503,
                    "automation_capacity",
                    "Лимит runtime-сеансов автоматизации",
                ));
            }
            let record = registry.policies.entry(key.clone()).or_default();
            if record.pending.is_some() {
                return Err(unavailable());
            }
            record.pending = Some(Pending {
                authority,
                check: check.clone(),
            });
        }
        let result = self
            .service
            .configure_with_check(kind, id, patch, check.clone())
            .await;
        // This owner, rather than the HTTP waiter, removes a rejected pending
        // receipt. Exact identity prevents removing another policy epoch.
        let mut registry = self.registry.lock().map_err(|_| unavailable())?;
        if let Some(record) = registry.policies.get_mut(&key)
            && record
                .pending
                .as_ref()
                .is_some_and(|pending| Arc::ptr_eq(&pending.check, &check))
            && let Some(pending) = record.pending.take()
        {
            pending.authority.revoke();
        }
        registry.prune();
        result
    }
}

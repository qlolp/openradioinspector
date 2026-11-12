use super::*;
use crate::{devices, error::ApiError, security::AuthReceipt};
use ori_archive::SessionLifecycleReceipt;
use ori_automation::AutomationScope;
use ori_guard::{ControlTarget, InstrumentControlGrant};
use std::collections::BTreeMap;

#[derive(Clone, Eq, PartialEq, Ord, PartialOrd)]
pub(super) struct Key {
    pub session: String,
    pub kind: PolicyKind,
}
impl Key {
    pub fn new(session: &str, kind: PolicyKind) -> Self {
        Self {
            session: session.into(),
            kind,
        }
    }
}
pub(super) struct Pending {
    pub authority: Arc<Authority>,
    pub check: Arc<dyn TuneCompletionCheck>,
}
#[derive(Default)]
pub(super) struct Record {
    pub active: Option<Arc<Authority>>,
    pub pending: Option<Pending>,
}
#[derive(Default)]
pub(super) struct Registry {
    pub policies: BTreeMap<Key, Record>,
    pub running: BTreeMap<Key, Weak<Running>>,
}
impl Registry {
    pub fn prune(&mut self) {
        self.policies
            .retain(|_, record| record.active.is_some() || record.pending.is_some());
        self.running.retain(|_, running| running.strong_count() > 0);
    }
    pub fn revoke_all(&self) {
        for record in self.policies.values() {
            if let Some(active) = &record.active {
                active.revoke();
            }
            if let Some(pending) = &record.pending {
                pending.authority.revoke();
            }
        }
    }
}
pub(super) fn api(error: ApiError) -> SessionError {
    SessionError::new(error.status.as_u16(), &error.code, error.message)
}
pub(super) fn archive(error: ori_archive::ArchiveError) -> SessionError {
    api(error.into())
}
pub(super) fn device(error: devices::DeviceError) -> SessionError {
    api(error.into())
}
pub(super) fn auth(receipt: &AuthReceipt) -> SessionResult<()> {
    receipt.revalidate().map_err(api)
}

/// No Debug/Serialize: retains the exact opt-in authority without publishing a
/// manual token or permitting a caller to refresh any original grant.
pub(super) struct Authority {
    pub key: Key,
    pub selected: Option<String>,
    pub enabled: bool,
    auth: AuthReceipt,
    pub grants: BTreeMap<String, InstrumentControlGrant>,
    closed: Arc<AtomicBool>,
    revoked: AtomicBool,
    _enrollment: SessionLifecycleReceipt,
}
pub(super) struct Admission {
    pub auth: AuthReceipt,
    pub control_owner: Option<String>,
    pub closed: Arc<AtomicBool>,
    pub enrollment: SessionLifecycleReceipt,
}
impl Authority {
    pub fn capture(
        core: &Core,
        kind: PolicyKind,
        id: &str,
        session: &Value,
        policy: &Value,
        admission: Admission,
    ) -> SessionResult<Arc<Self>> {
        let Admission {
            auth: receipt,
            control_owner,
            closed,
            enrollment,
        } = admission;
        let control_owner = control_owner.as_deref();
        auth(&receipt)?;
        let enabled = policy["enabled"].as_bool().ok_or_else(unavailable)?;
        let selected = policy[if kind == PolicyKind::Detection {
            "stream_id"
        } else {
            "receiver_stream_id"
        }]
        .as_str()
        .map(str::to_owned);
        if control_owner.is_some() && selected.is_none() {
            return Err(SessionError::invalid(
                "control_owner требует явно выбранный поток",
            ));
        }
        let role = if kind == PolicyKind::Detection {
            "spectrum_analyzer"
        } else {
            "receiver"
        };
        let mut grants = BTreeMap::new();
        let guard = &core.domains.get().ok_or_else(unavailable)?.guard;
        for stream in session["streams"].as_array().ok_or_else(unavailable)? {
            let stream_id = stream["id"].as_str().ok_or_else(unavailable)?;
            if stream["role"] != role
                || selected
                    .as_ref()
                    .is_some_and(|selected| selected != stream_id)
            {
                continue;
            }
            let grant = guard
                .capture_instrument_control(
                    &ControlTarget {
                        session_id: id.into(),
                        stream_id: stream_id.into(),
                    },
                    control_owner,
                )
                .map_err(|_| denied())?;
            grants.insert(stream_id.into(), grant);
        }
        let authority = Arc::new(Self {
            key: Key::new(id, kind),
            selected,
            enabled,
            auth: receipt,
            grants,
            closed,
            revoked: AtomicBool::new(false),
            _enrollment: enrollment,
        });
        authority.revalidate()?;
        Ok(authority)
    }
    pub fn revoke(&self) {
        self.revoked.store(true, Ordering::Release);
    }
    pub fn matches(&self, scope: &AutomationScope) -> bool {
        self.key.session == scope.session_id
            && self.key.kind == scope.kind
            && self.selected == scope.stream_id
    }
}
impl TuneCompletionCheck for Authority {
    fn revalidate(&self) -> SessionResult<()> {
        if self.closed.load(Ordering::Acquire) || self.revoked.load(Ordering::Acquire) {
            return Err(denied());
        }
        auth(&self.auth)?;
        for grant in self.grants.values() {
            grant.check().map_err(|_| denied())?;
        }
        Ok(())
    }
}
pub(super) struct Running {
    pub policy: Arc<Authority>,
    pub original: Arc<dyn TuneCompletionCheck>,
    pub session: SessionLifecycleReceipt,
    pub workers: BTreeMap<String, devices::RetainedWorkerHandle>,
}
impl TuneCompletionCheck for Running {
    fn revalidate(&self) -> SessionResult<()> {
        self.original.revalidate()?;
        self.policy.revalidate()?;
        if !self.session.check() {
            return Err(denied());
        }
        for worker in self.workers.values() {
            worker.check().map_err(device)?;
        }
        Ok(())
    }
}

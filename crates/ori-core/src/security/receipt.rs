//! Immutable originating request authority. No token, I/O or permit renewal.
use crate::error::ApiResult;
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
use tokio::sync::Notify;

pub(super) struct Revocation {
    live: AtomicBool,
    changed: Notify,
}
impl Revocation {
    pub(super) fn live() -> Arc<Self> {
        Arc::new(Self {
            live: AtomicBool::new(true),
            changed: Notify::new(),
        })
    }
    pub(super) fn active(&self) -> bool {
        self.live.load(Ordering::Acquire)
    }
    pub(super) fn revoke(&self) {
        if self.live.swap(false, Ordering::AcqRel) {
            self.changed.notify_waiters();
        }
    }
}
/// Deliberately no Debug/Serialize: preserve the original credential epoch and
/// server lifetime without exposing or copying its credential into actors.
#[derive(Clone)]
pub(crate) struct AuthReceipt {
    server: Arc<Revocation>,
    credential: Option<Arc<Revocation>>,
}
impl AuthReceipt {
    pub(super) fn new(server: Arc<Revocation>, credential: Option<Arc<Revocation>>) -> Self {
        Self { server, credential }
    }
    pub(crate) fn revalidate(&self) -> ApiResult<()> {
        if self.server.active() && self.credential.as_ref().is_none_or(|flag| flag.active()) {
            Ok(())
        } else {
            Err(super::denial())
        }
    }
    pub(super) fn revoke_credential(&self) {
        if let Some(flag) = &self.credential {
            flag.revoke();
        }
    }
    /// Cancellation wake for silent pending I/O; the cached check alone cannot
    /// wake an adapter whose byte stream has not become readable/writable.
    pub(crate) async fn revoked(&self) {
        loop {
            let server = self.server.changed.notified();
            tokio::pin!(server);
            server.as_mut().enable();
            if let Some(flag) = &self.credential {
                let credential = flag.changed.notified();
                tokio::pin!(credential);
                credential.as_mut().enable();
                if self.revalidate().is_err() {
                    return;
                }
                tokio::select! {_=server=>{},_=credential=>{}}
            } else {
                if self.revalidate().is_err() {
                    return;
                }
                server.await;
            }
        }
    }
}

//! Original authority is activated only between canonical ACK and dispatch.
use ori_archive::{Archive, ArchiveConfig, CommitNotice};
use ori_automation::{
    AutomationConfig, AutomationEffects, AutomationScope, AutomationService, DetectedRegion,
    DetectionRequest, PolicyKind,
};
use ori_runtime::worker::Cancellation;
use ori_session::{
    SessionEffects, SessionError, SessionFuture, SessionService, TuneCompletionCheck, TuneReceipt,
    TuneRequest,
};
use serde_json::{Value, json};
use std::sync::{
    Arc, Condvar, Mutex,
    atomic::{AtomicBool, AtomicUsize, Ordering},
};
use tokio::sync::{Semaphore, broadcast};

const SID: &str = "session-é";
fn denied() -> SessionError {
    SessionError::new(409, "original_revoked", "Original authority revoked")
}
struct AdmissionBarrier {
    entered: Semaphore,
    release: Mutex<bool>,
    wake: Condvar,
}
impl AdmissionBarrier {
    fn pause(&self) {
        self.entered.add_permits(1);
        let mut released = self.release.lock().unwrap();
        while !*released {
            released = self.wake.wait(released).unwrap();
        }
    }
    fn release(&self) {
        *self.release.lock().unwrap() = true;
        self.wake.notify_all();
    }
}
struct ReleaseOnDrop(Option<Arc<AdmissionBarrier>>);
impl Drop for ReleaseOnDrop {
    fn drop(&mut self) {
        if let Some(barrier) = &self.0 {
            barrier.release();
        }
    }
}
struct Original {
    live: AtomicBool,
    checks: AtomicUsize,
    barrier: Option<Arc<AdmissionBarrier>>,
}
impl TuneCompletionCheck for Original {
    fn revalidate(&self) -> Result<(), SessionError> {
        if !self.live.load(Ordering::Acquire) {
            return Err(denied());
        }
        let count = self.checks.fetch_add(1, Ordering::SeqCst) + 1;
        // Only the fixture pauses the successful last admission check. This
        // exposes revocation after that decision and before the Event commit.
        if count == 2
            && let Some(barrier) = &self.barrier
        {
            barrier.pause();
        }
        Ok(())
    }
}
struct Effects {
    original: Arc<dyn TuneCompletionCheck>,
    active: AtomicBool,
    commit_seen: AtomicBool,
    activations: AtomicUsize,
    captures: AtomicUsize,
    notices: Mutex<broadcast::Receiver<CommitNotice>>,
}
impl AutomationEffects for Effects {
    fn policy_committed(
        &self,
        scope: AutomationScope,
        original: Option<Arc<dyn TuneCompletionCheck>>,
    ) -> Result<(), SessionError> {
        assert_eq!(scope.session_id, SID);
        assert_eq!(scope.kind, PolicyKind::Completion);
        assert!(scope.stream_id.is_none());
        let notice = self.notices.lock().unwrap().try_recv().unwrap();
        assert!(notice.first_sequence <= notice.last_sequence);
        self.commit_seen.store(true, Ordering::Release);
        let original = original.expect("Checked configuration retains its receipt");
        assert!(Arc::ptr_eq(&original, &self.original));
        original.revalidate()?;
        self.active.store(true, Ordering::Release);
        self.activations.fetch_add(1, Ordering::SeqCst);
        Ok(())
    }
    fn capture(&self, _: AutomationScope) -> Result<Arc<dyn TuneCompletionCheck>, SessionError> {
        self.captures.fetch_add(1, Ordering::SeqCst);
        if !self.active.load(Ordering::Acquire) {
            return Err(SessionError::new(
                409,
                "authority_pending",
                "Awaiting canonical policy ACK",
            ));
        }
        self.original.revalidate()?;
        Ok(self.original.clone())
    }
    fn detect(
        &self,
        _: DetectionRequest,
        _: Arc<dyn TuneCompletionCheck>,
        _: Cancellation,
    ) -> SessionFuture<'_, Vec<DetectedRegion>> {
        Box::pin(async { panic!("No scientific operation was requested") })
    }
    fn tune(
        &self,
        _: TuneRequest,
        _: Arc<dyn TuneCompletionCheck>,
        _: Cancellation,
    ) -> SessionFuture<'_, TuneReceipt> {
        Box::pin(async { panic!("No device operation was requested") })
    }
}
impl SessionEffects for Effects {
    fn tune(&self, _: TuneRequest) -> SessionFuture<'_, TuneReceipt> {
        Box::pin(async { panic!("No device operation was requested") })
    }
    fn validate_reference(&self, _: Value, _: Value) -> SessionFuture<'_, ()> {
        Box::pin(async { panic!("No reference operation was requested") })
    }
}
struct Fixture {
    _directory: tempfile::TempDir,
    archive: Archive,
    automation: AutomationService,
    effects: Arc<Effects>,
    original: Arc<Original>,
}
impl Fixture {
    async fn new(barrier: Option<Arc<AdmissionBarrier>>) -> Self {
        let directory = tempfile::tempdir().unwrap();
        let archive =
            Archive::open(ArchiveConfig::new(directory.path().join("archive.sqlite3"))).unwrap();
        let source: Value = serde_json::from_str(include_str!(
            "../../../contracts/fixtures/automation-domain-v1.json"
        ))
        .unwrap();
        archive
            .create_session(source["cases"][0]["seed"]["sessions"][0].clone())
            .await
            .unwrap();
        let original = Arc::new(Original {
            live: AtomicBool::new(true),
            checks: AtomicUsize::new(0),
            barrier,
        });
        let effects = Arc::new(Effects {
            original: original.clone(),
            active: AtomicBool::new(false),
            commit_seen: AtomicBool::new(false),
            activations: AtomicUsize::new(0),
            captures: AtomicUsize::new(0),
            notices: Mutex::new(archive.subscribe_commits()),
        });
        let sessions = SessionService::new(archive.clone(), effects.clone());
        let automation = AutomationService::new(
            archive.clone(),
            sessions,
            effects.clone(),
            AutomationConfig {
                start_background: false,
                ..AutomationConfig::default()
            },
        )
        .unwrap();
        Self {
            _directory: directory,
            archive,
            automation,
            effects,
            original,
        }
    }
    async fn close(&self) {
        self.automation.close().await.unwrap();
        self.archive.close().await.unwrap();
    }
}
async fn enqueue<F: std::future::Future>(mut future: std::pin::Pin<&mut F>) {
    assert!(
        std::future::poll_fn(|cx| std::task::Poll::Ready(future.as_mut().poll(cx).is_pending()))
            .await
    );
}

#[tokio::test]
async fn canonical_policy_ack_activates_the_same_original_before_first_queued_dispatch() {
    let f = Fixture::new(None).await;
    let coordinator = f.archive.domain_coordinator();
    let held = coordinator.lock().await;
    let configuring = f.automation.configure_with_check(
        PolicyKind::Completion,
        SID,
        json!({"enabled":true}),
        f.original.clone(),
    );
    tokio::pin!(configuring);
    enqueue(configuring.as_mut()).await;
    let running = f.automation.run_once(PolicyKind::Completion, SID);
    tokio::pin!(running);
    enqueue(running.as_mut()).await;
    assert!(!f.effects.active.load(Ordering::Acquire));
    assert!(!f.effects.commit_seen.load(Ordering::Acquire));
    drop(held);
    let (configured, dispatched) = tokio::join!(configuring, running);
    let events = f.archive.events(0, 100).await.unwrap();
    let status = f
        .automation
        .status(PolicyKind::Completion, SID)
        .await
        .unwrap();
    f.close().await;
    assert_eq!(configured.unwrap()["policy"]["enabled"], true);
    assert!(
        dispatched.is_ok(),
        "First dispatch must use its original authority after canonical ACK: {dispatched:?}"
    );
    assert_eq!(dispatched.unwrap(), Value::Null);
    assert_eq!(status["runtime"]["last_result"], "idle");
    assert_eq!(f.effects.activations.load(Ordering::SeqCst), 1);
    assert_eq!(f.effects.captures.load(Ordering::SeqCst), 1);
    assert!(
        events
            .iter()
            .any(|event| event["event_type"] == "automatic_completion.policy_changed")
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn revocation_after_admission_cannot_activate_a_fresh_receipt_after_commit() {
    let barrier = Arc::new(AdmissionBarrier {
        entered: Semaphore::new(0),
        release: Mutex::new(false),
        wake: Condvar::new(),
    });
    let release = ReleaseOnDrop(Some(barrier.clone()));
    let f = Fixture::new(Some(barrier.clone())).await;
    let automation = f.automation.clone();
    let original = f.original.clone();
    let configuring = tokio::spawn(async move {
        automation
            .configure_with_check(
                PolicyKind::Completion,
                SID,
                json!({"enabled":true}),
                original,
            )
            .await
    });
    barrier.entered.acquire().await.unwrap().forget();
    f.original.live.store(false, Ordering::Release);
    barrier.release();
    let configured = configuring.await.unwrap();
    let dispatched = f.automation.run_once(PolicyKind::Completion, SID).await;
    let events = f.archive.events(0, 100).await.unwrap();
    let status = f
        .automation
        .status(PolicyKind::Completion, SID)
        .await
        .unwrap();
    f.close().await;
    drop(release);
    assert!(
        configured.is_err(),
        "The committed callback must reject original revoked authority"
    );
    assert!(dispatched.is_err());
    assert!(f.effects.commit_seen.load(Ordering::Acquire));
    assert_eq!(f.effects.activations.load(Ordering::SeqCst), 0);
    assert!(!f.effects.active.load(Ordering::Acquire));
    // The canonical event really committed. A rejected post-ACK callback does
    // not invent rollback or pretend the configured policy was never stored.
    assert_eq!(status["policy"]["enabled"], true);
    assert_eq!(
        events
            .iter()
            .filter(|event| event["event_type"] == "automatic_completion.policy_changed")
            .count(),
        1
    );
}

#[tokio::test]
async fn pure_policy_patch_validation_preserves_original_rules_without_admitting_a_policy() {
    let f = Fixture::new(None).await;
    let session = f.archive.get_session(SID).await.unwrap().unwrap();
    let before_events = f.archive.events(0, 100).await.unwrap();
    let patch = AutomationService::validated_policy_patch(
        PolicyKind::Completion,
        json!({"enabled":true,"minimum_listening_seconds":" ١_٠ "}),
        &session,
    )
    .unwrap();
    let unknown = AutomationService::validated_policy_patch(
        PolicyKind::Completion,
        json!({"unknown":true}),
        &session,
    )
    .unwrap_err();
    let oversized = AutomationService::validated_policy_patch(
        PolicyKind::Completion,
        json!({"minimum_listening_seconds":"1".repeat(16385)}),
        &session,
    )
    .unwrap_err();
    let status = f
        .automation
        .status(PolicyKind::Completion, SID)
        .await
        .unwrap();
    let events = f.archive.events(0, 100).await.unwrap();
    let saved = f.archive.get_session(SID).await.unwrap().unwrap();
    f.close().await;
    assert_eq!(
        patch,
        json!({"enabled":true,"minimum_listening_seconds":10.0})
    );
    assert_eq!(unknown.message, "Неизвестные поля политики: unknown");
    assert_eq!(oversized.message, "Слишком большая политика");
    assert_eq!(saved, session);
    assert_eq!(status["policy"]["enabled"], false);
    assert_eq!(events, before_events);
    assert_eq!(f.effects.activations.load(Ordering::SeqCst), 0);
    assert_eq!(f.effects.captures.load(Ordering::SeqCst), 0);
}

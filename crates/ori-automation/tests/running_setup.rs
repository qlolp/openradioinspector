//! Running setup is owned per dispatch; Draft policies have no execution proof.
use ori_archive::{Archive, ArchiveConfig, SessionLifecycleReceipt};
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
use tokio::sync::Semaphore;

struct PolicyCheck(Arc<AtomicBool>);
impl TuneCompletionCheck for PolicyCheck {
    fn revalidate(&self) -> Result<(), SessionError> {
        if self.0.load(Ordering::Acquire) {
            Ok(())
        } else {
            Err(SessionError::new(
                409,
                "original_revoked",
                "Original authority revoked",
            ))
        }
    }
}
struct RunningCheck {
    original: Arc<dyn TuneCompletionCheck>,
    session: SessionLifecycleReceipt,
}
impl TuneCompletionCheck for RunningCheck {
    fn revalidate(&self) -> Result<(), SessionError> {
        self.original.revalidate()?;
        if self.session.check() {
            Ok(())
        } else {
            Err(SessionError::new(
                409,
                "session_changed",
                "Original lifecycle changed",
            ))
        }
    }
}
struct SetupBarrier {
    entered: Semaphore,
    released: Mutex<bool>,
    wake: Condvar,
}
impl SetupBarrier {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            entered: Semaphore::new(0),
            released: Mutex::new(false),
            wake: Condvar::new(),
        })
    }
    fn pause(&self) {
        self.entered.add_permits(1);
        let mut released = self.released.lock().unwrap();
        while !*released {
            released = self.wake.wait(released).unwrap();
        }
    }
    fn release(&self) {
        *self.released.lock().unwrap() = true;
        self.wake.notify_all();
    }
}
struct ReleaseOnDrop(Arc<SetupBarrier>);
impl Drop for ReleaseOnDrop {
    fn drop(&mut self) {
        self.0.release();
    }
}
struct Effects {
    archive: Archive,
    live: Arc<AtomicBool>,
    setup: AtomicUsize,
    effects: AtomicUsize,
    barrier: Option<Arc<SetupBarrier>>,
    replace_original: AtomicBool,
}
impl AutomationEffects for Effects {
    fn capture(&self, _: AutomationScope) -> Result<Arc<dyn TuneCompletionCheck>, SessionError> {
        let check = Arc::new(PolicyCheck(self.live.clone()));
        check.revalidate()?;
        Ok(check)
    }
    fn setup_running(
        &self,
        scope: AutomationScope,
        original: Arc<dyn TuneCompletionCheck>,
    ) -> Result<Arc<dyn TuneCompletionCheck>, SessionError> {
        self.setup.fetch_add(1, Ordering::SeqCst);
        // Only this private fixture pauses between the library's real Running
        // read and cache capture, so an actual committed stop can race setup.
        if scope.session_id == "setup-a"
            && let Some(barrier) = &self.barrier
        {
            barrier.pause();
        }
        original.revalidate()?;
        let session = self
            .archive
            .cached_session_lifecycle(&scope.session_id)
            .ok_or_else(|| {
                SessionError::new(409, "session_changed", "Original lifecycle changed")
            })?;
        let original = if self.replace_original.swap(false, Ordering::AcqRel) {
            // An adversarial provider returns a fresh permission after revoking
            // the old one. The library must still retain the original check.
            self.live.store(false, Ordering::Release);
            Arc::new(PolicyCheck(Arc::new(AtomicBool::new(true)))) as Arc<dyn TuneCompletionCheck>
        } else {
            original
        };
        let check = Arc::new(RunningCheck { original, session });
        check.revalidate()?;
        Ok(check)
    }
    fn detect(
        &self,
        _: DetectionRequest,
        _: Arc<dyn TuneCompletionCheck>,
        _: Cancellation,
    ) -> SessionFuture<'_, Vec<DetectedRegion>> {
        self.effects.fetch_add(1, Ordering::SeqCst);
        Box::pin(async {
            Err(SessionError::new(
                503,
                "unexpected_effect",
                "Detection not requested",
            ))
        })
    }
    fn tune(
        &self,
        _: TuneRequest,
        _: Arc<dyn TuneCompletionCheck>,
        _: Cancellation,
    ) -> SessionFuture<'_, TuneReceipt> {
        self.effects.fetch_add(1, Ordering::SeqCst);
        Box::pin(async {
            Err(SessionError::new(
                503,
                "unexpected_effect",
                "Tune not requested",
            ))
        })
    }
}
impl SessionEffects for Effects {
    fn tune(&self, _: TuneRequest) -> SessionFuture<'_, TuneReceipt> {
        Box::pin(async {
            Err(SessionError::new(
                503,
                "unexpected_effect",
                "Tune not requested",
            ))
        })
    }
    fn validate_reference(&self, _: Value, _: Value) -> SessionFuture<'_, ()> {
        Box::pin(async {
            Err(SessionError::new(
                503,
                "unexpected_effect",
                "Reference not requested",
            ))
        })
    }
}
fn session(id: &str, running: bool) -> Value {
    let oracle: Value = serde_json::from_str(include_str!(
        "../../../contracts/fixtures/automation-domain-v1.json"
    ))
    .unwrap();
    let mut value = oracle["cases"][0]["seed"]["sessions"][0].clone();
    value["id"] = json!(id);
    value["state"] = json!(if running { "running" } else { "draft" });
    value["started_at"] = if running {
        json!("2026-11-11T12:00:00Z")
    } else {
        Value::Null
    };
    value["ended_at"] = Value::Null;
    for stream in value["streams"].as_array_mut().unwrap() {
        let original = stream["id"].as_str().unwrap().to_owned();
        stream["id"] = json!(format!("{id}-{original}"));
        stream["session_id"] = json!(id);
    }
    value
}
fn service(
    archive: &Archive,
    barrier: Option<Arc<SetupBarrier>>,
) -> (AutomationService, Arc<Effects>) {
    service_with_limit(archive, barrier, 64)
}
fn service_with_limit(
    archive: &Archive,
    barrier: Option<Arc<SetupBarrier>>,
    max_sessions: usize,
) -> (AutomationService, Arc<Effects>) {
    service_configured(archive, barrier, max_sessions, false)
}
fn service_configured(
    archive: &Archive,
    barrier: Option<Arc<SetupBarrier>>,
    max_sessions: usize,
    start_background: bool,
) -> (AutomationService, Arc<Effects>) {
    let effects = Arc::new(Effects {
        archive: archive.clone(),
        live: Arc::new(AtomicBool::new(true)),
        setup: AtomicUsize::new(0),
        effects: AtomicUsize::new(0),
        barrier,
        replace_original: AtomicBool::new(false),
    });
    let sessions = SessionService::new(archive.clone(), effects.clone());
    let automation = AutomationService::new(
        archive.clone(),
        sessions,
        effects.clone(),
        AutomationConfig {
            start_background,
            max_sessions,
            max_in_flight: (max_sessions * 3).min(16),
            ..AutomationConfig::default()
        },
    )
    .unwrap();
    (automation, effects)
}

#[tokio::test]
async fn draft_opt_in_stays_idle_then_captures_running_setup_after_explicit_start() {
    let directory = tempfile::tempdir().unwrap();
    let archive = Archive::open(ArchiveConfig::new(
        directory.path().join("sessions.sqlite3"),
    ))
    .unwrap();
    archive
        .create_session(session("setup-a", false))
        .await
        .unwrap();
    let enrollment = archive.capture_session_lifecycle("setup-a").await.unwrap();
    assert!(!enrollment.check());
    let (automation, effects) = service(&archive, None);
    automation
        .configure(PolicyKind::Completion, "setup-a", json!({"enabled":true}))
        .await
        .unwrap();
    assert_eq!(
        automation
            .run_once(PolicyKind::Completion, "setup-a")
            .await
            .unwrap(),
        Value::Null
    );
    assert_eq!(effects.setup.load(Ordering::SeqCst), 0);
    assert_eq!(
        automation
            .status(PolicyKind::Completion, "setup-a")
            .await
            .unwrap()["runtime"]["last_result"],
        "session_not_running"
    );
    archive
        .save_session(session("setup-a", true), None)
        .await
        .unwrap();
    assert_eq!(
        automation
            .run_once(PolicyKind::Completion, "setup-a")
            .await
            .unwrap(),
        Value::Null
    );
    let setup = effects.setup.load(Ordering::SeqCst);
    let effects_called = effects.effects.load(Ordering::SeqCst);
    automation.close().await.unwrap();
    archive.close().await.unwrap();
    assert_eq!(
        setup, 1,
        "Running dispatch must capture its actual execution setup"
    );
    assert_eq!(effects_called, 0);
    assert!(
        !enrollment.check(),
        "Draft enrollment must never become Running authority"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn actual_stop_during_setup_blocks_that_dispatch_without_serializing_another_session() {
    let directory = tempfile::tempdir().unwrap();
    let archive = Archive::open(ArchiveConfig::new(
        directory.path().join("sessions.sqlite3"),
    ))
    .unwrap();
    for id in ["setup-a", "setup-b"] {
        archive.create_session(session(id, true)).await.unwrap();
    }
    let first_enrollment = archive.capture_session_lifecycle("setup-a").await.unwrap();
    let second_enrollment = archive.capture_session_lifecycle("setup-b").await.unwrap();
    let barrier = SetupBarrier::new();
    let release = ReleaseOnDrop(barrier.clone());
    let (automation, effects) = service(&archive, Some(barrier.clone()));
    for id in ["setup-a", "setup-b"] {
        automation
            .configure(PolicyKind::Completion, id, json!({"enabled":true}))
            .await
            .unwrap();
    }
    let owner = automation.clone();
    let mut first =
        tokio::spawn(async move { owner.run_once(PolicyKind::Completion, "setup-a").await });
    tokio::select! {
        entered=tokio::time::timeout(std::time::Duration::from_secs(2), barrier.entered.acquire())=>entered.unwrap().unwrap().forget(),
        result=&mut first=>{
            automation.close().await.unwrap();
            archive.close().await.unwrap();
            panic!("Running dispatch completed without entering setup: {result:?}");
        },
    }
    let mut stopped = session("setup-a", true);
    stopped["state"] = json!("stopped");
    stopped["ended_at"] = json!("2026-11-11T12:00:01Z");
    archive
        .stop_session(stopped, "2026-11-11T12:00:01Z")
        .await
        .unwrap();
    assert!(!first_enrollment.check());
    assert!(second_enrollment.check());
    let second = tokio::time::timeout(
        std::time::Duration::from_secs(2),
        automation.run_once(PolicyKind::Completion, "setup-b"),
    )
    .await
    .unwrap();
    barrier.release();
    let first = first.await.unwrap();
    let setup = effects.setup.load(Ordering::SeqCst);
    let effects_called = effects.effects.load(Ordering::SeqCst);
    automation.close().await.unwrap();
    archive.close().await.unwrap();
    drop(release);
    assert!(
        first.is_err(),
        "Committed stop between Running read and setup must deny the old dispatch"
    );
    assert_eq!(second.unwrap(), Value::Null);
    assert_eq!(setup, 2);
    assert_eq!(effects_called, 0);
}

#[tokio::test]
async fn running_setup_cannot_replace_the_revoked_original_policy_authority() {
    let directory = tempfile::tempdir().unwrap();
    let archive = Archive::open(ArchiveConfig::new(
        directory.path().join("sessions.sqlite3"),
    ))
    .unwrap();
    archive
        .create_session(session("setup-a", true))
        .await
        .unwrap();
    let enrollment = archive.capture_session_lifecycle("setup-a").await.unwrap();
    let (automation, effects) = service(&archive, None);
    automation
        .configure(PolicyKind::Completion, "setup-a", json!({"enabled":true}))
        .await
        .unwrap();
    effects.replace_original.store(true, Ordering::Release);
    let result = automation.run_once(PolicyKind::Completion, "setup-a").await;
    let called = effects.effects.load(Ordering::SeqCst);
    automation.close().await.unwrap();
    archive.close().await.unwrap();
    assert!(
        result.is_err(),
        "A fresh setup check cannot replace original revoked authority"
    );
    assert_eq!(called, 0);
    assert!(!enrollment.check());
}

#[tokio::test]
async fn queued_revoked_opt_in_cannot_change_policy_or_publish_an_event() {
    let directory = tempfile::tempdir().unwrap();
    let archive = Archive::open(ArchiveConfig::new(
        directory.path().join("sessions.sqlite3"),
    ))
    .unwrap();
    archive
        .create_session(session("setup-a", false))
        .await
        .unwrap();
    archive
        .create_session(session("setup-b", false))
        .await
        .unwrap();
    let (automation, effects) = service_with_limit(&archive, None, 1);
    let coordinator = archive.domain_coordinator();
    let held = coordinator.lock().await;
    let original = Arc::new(PolicyCheck(effects.live.clone()));
    let configuring = automation.configure_with_check(
        PolicyKind::Completion,
        "setup-a",
        json!({"enabled":true}),
        original,
    );
    tokio::pin!(configuring);
    let pending = std::future::poll_fn(|cx| {
        std::task::Poll::Ready(std::future::Future::poll(configuring.as_mut(), cx).is_pending())
    })
    .await;
    assert!(pending, "The request must be admitted before revocation");
    effects.live.store(false, Ordering::Release);
    drop(held);
    let result = configuring.await;
    let status = automation
        .status(PolicyKind::Completion, "setup-a")
        .await
        .unwrap();
    let events = archive.events(0, 100).await.unwrap();
    let valid = Arc::new(PolicyCheck(Arc::new(AtomicBool::new(true))));
    let independent = automation
        .configure_with_check(
            PolicyKind::Completion,
            "setup-b",
            json!({"enabled":true}),
            valid,
        )
        .await;
    automation.close().await.unwrap();
    archive.close().await.unwrap();
    assert!(
        result.is_err(),
        "A queued request cannot use its pre-wait authority after revocation"
    );
    assert_eq!(status["policy"]["enabled"], false);
    assert!(
        !events
            .iter()
            .any(|event| event["event_type"] == "automatic_completion.policy_changed")
    );
    assert!(
        independent.is_ok(),
        "Rejected admission cannot consume another session's policy capacity"
    );
}

#[tokio::test]
async fn enabling_one_kind_preserves_other_kinds_original_scheduler_status() {
    let directory = tempfile::tempdir().unwrap();
    let archive = Archive::open(ArchiveConfig::new(
        directory.path().join("sessions.sqlite3"),
    ))
    .unwrap();
    archive
        .create_session(session("setup-a", false))
        .await
        .unwrap();
    let (automation, effects) = service_configured(&archive, None, 64, true);
    let enabled = automation
        .configure(PolicyKind::Detection, "setup-a", json!({"enabled":true}))
        .await
        .unwrap();
    let handoff = automation
        .status(PolicyKind::Handoff, "setup-a")
        .await
        .unwrap();
    let completion = automation
        .status(PolicyKind::Completion, "setup-a")
        .await
        .unwrap();
    automation.close().await.unwrap();
    archive.close().await.unwrap();
    assert_eq!(enabled["scheduler_alive"], true);
    assert_eq!(
        handoff["scheduler_alive"], false,
        "Python starts each policy kind independently"
    );
    assert_eq!(completion["scheduler_alive"], false);
    assert_eq!(effects.effects.load(Ordering::SeqCst), 0);
}

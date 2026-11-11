use chrono::{DateTime, Utc};
use ori_archive::{Archive, ArchiveConfig};
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
    Arc,
    atomic::{AtomicBool, AtomicUsize, Ordering},
};
use std::time::Duration;
use tokio::sync::Notify;

const SID: &str = "session-é";
const STREAM: &str = "spectrum/e\u{301}";
const RECEIVER: &str = "receiver/é";
struct Original {
    revision: Arc<AtomicUsize>,
    expected: usize,
}
impl TuneCompletionCheck for Original {
    fn revalidate(&self) -> Result<(), SessionError> {
        if self.revision.load(Ordering::SeqCst) == self.expected {
            Ok(())
        } else {
            Err(SessionError::new(
                409,
                "stale_receipt",
                "Original receipt invalidated",
            ))
        }
    }
}
struct Effects {
    revision: Arc<AtomicUsize>,
    captures: AtomicUsize,
    active: AtomicUsize,
    block_science: AtomicBool,
    block_tune: AtomicBool,
    entered: Notify,
    release: Notify,
}
impl Effects {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            revision: Arc::new(AtomicUsize::new(0)),
            captures: AtomicUsize::new(0),
            active: AtomicUsize::new(0),
            block_science: AtomicBool::new(false),
            block_tune: AtomicBool::new(false),
            entered: Notify::new(),
            release: Notify::new(),
        })
    }
    fn receipt(&self) -> Arc<dyn TuneCompletionCheck> {
        Arc::new(Original {
            revision: self.revision.clone(),
            expected: self.revision.load(Ordering::SeqCst),
        })
    }
    async fn wait(&self, blocked: bool) {
        if blocked {
            self.active.fetch_add(1, Ordering::SeqCst);
            self.entered.notify_one();
            self.release.notified().await;
            self.active.fetch_sub(1, Ordering::SeqCst);
        }
    }
}
impl SessionEffects for Effects {
    fn tune(&self, _: TuneRequest) -> SessionFuture<'_, TuneReceipt> {
        Box::pin(async { Ok(TuneReceipt::new(self.receipt())) })
    }
    fn validate_reference(&self, _: Value, _: Value) -> SessionFuture<'_, ()> {
        Box::pin(async { Ok(()) })
    }
}
impl AutomationEffects for Effects {
    fn capture(&self, _: AutomationScope) -> Result<Arc<dyn TuneCompletionCheck>, SessionError> {
        self.captures.fetch_add(1, Ordering::SeqCst);
        Ok(self.receipt())
    }
    fn detect(
        &self,
        _: DetectionRequest,
        check: Arc<dyn TuneCompletionCheck>,
        _: Cancellation,
    ) -> SessionFuture<'_, Vec<DetectedRegion>> {
        Box::pin(async move {
            self.wait(self.block_science.load(Ordering::SeqCst)).await;
            check.revalidate()?;
            Ok(vec![DetectedRegion {
                center_frequency_hz: 100100001,
                bandwidth_hz: 100000,
                peak_level: -0.0,
                score: 60.0,
            }])
        })
    }
    fn tune(
        &self,
        _: TuneRequest,
        check: Arc<dyn TuneCompletionCheck>,
        _: Cancellation,
    ) -> SessionFuture<'_, TuneReceipt> {
        Box::pin(async move {
            self.wait(self.block_tune.load(Ordering::SeqCst)).await;
            check.revalidate()?;
            Ok(TuneReceipt::new(check))
        })
    }
}
struct Fixture {
    _directory: tempfile::TempDir,
    archive: Archive,
    session: Arc<SessionService>,
    automation: AutomationService,
    effects: Arc<Effects>,
}
impl Fixture {
    async fn new() -> Self {
        let directory = tempfile::tempdir().unwrap();
        let archive =
            Archive::open(ArchiveConfig::new(directory.path().join("archive.sqlite"))).unwrap();
        let source: Value = serde_json::from_str(include_str!(
            "../../../contracts/fixtures/automation-domain-v1.json"
        ))
        .unwrap();
        archive
            .create_session(source["cases"][0]["seed"]["sessions"][0].clone())
            .await
            .unwrap();
        let effects = Effects::new();
        let now = Arc::new(|| {
            "2026-11-11T12:00:00.123456Z"
                .parse::<DateTime<Utc>>()
                .unwrap()
        });
        let ids = Arc::new(AtomicUsize::new(0));
        let session = SessionService::with_sources(
            archive.clone(),
            effects.clone(),
            now.clone(),
            Arc::new(move || format!("c{}", ids.fetch_add(1, Ordering::SeqCst))),
        );
        let automation = AutomationService::with_sources(
            archive.clone(),
            session.clone(),
            effects.clone(),
            AutomationConfig {
                start_background: false,
                ..AutomationConfig::default()
            },
            now,
            Arc::new(|| 0.0),
        )
        .unwrap();
        Self {
            _directory: directory,
            archive,
            session,
            automation,
            effects,
        }
    }
    async fn source(&self, id: &str) {
        let mut source: Value = serde_json::from_str(include_str!(
            "../../../contracts/fixtures/automation-domain-v1.json"
        ))
        .unwrap();
        let mut value = source["regions"][0]["sweep"].take();
        value["id"] = json!(id);
        self.archive.append_sweeps(vec![value]).await.unwrap();
    }
    async fn queued(&self, id: &str, priority: u64) {
        self.session.create_candidate(SID,json!({"id":format!("candidate-{id}"),"source_stream_id":STREAM,"interval":{"started_at":"2026-11-11T12:00:00.123456Z","ended_at":"2026-11-11T12:00:00.123456Z"},"center_frequency_hz":100100001,"level_unit":"dBm","peak_level":-0.0})).await.unwrap();
        self.session
            .enqueue_investigation(
                SID,
                &format!("candidate-{id}"),
                json!({"id":id,"target_stream_id":RECEIVER,"mode":"FM","priority":priority}),
            )
            .await
            .unwrap();
    }
    async fn close(self) {
        self.automation.close().await.unwrap();
        self.archive.close().await.unwrap();
    }
}

#[tokio::test]
async fn disable_reenable_during_science_cannot_commit_under_new_policy_epoch() {
    let f = Fixture::new().await;
    f.automation
        .configure(
            PolicyKind::Detection,
            SID,
            json!({"enabled":true,"threshold_dbm":-60.0}),
        )
        .await
        .unwrap();
    f.source("é").await;
    f.effects.block_science.store(true, Ordering::SeqCst);
    let client = f.automation.clone();
    let running = tokio::spawn(async move { client.run_once(PolicyKind::Detection, SID).await });
    f.effects.entered.notified().await;
    for enabled in [false, true] {
        tokio::time::timeout(
            Duration::from_secs(2),
            f.automation
                .configure(PolicyKind::Detection, SID, json!({"enabled":enabled})),
        )
        .await
        .expect("configuration must not wait on science")
        .unwrap();
    }
    f.effects.release.notify_one();
    let _ = running.await.unwrap();
    assert!(
        f.session
            .list_candidates(SID, None)
            .await
            .unwrap()
            .as_array()
            .unwrap()
            .is_empty()
    );
    assert_eq!(
        f.effects.captures.load(Ordering::SeqCst),
        1,
        "no fresh permit after delayed science"
    );
    f.effects.block_science.store(false, Ordering::SeqCst);
    assert_eq!(
        f.automation
            .run_once(PolicyKind::Detection, SID)
            .await
            .unwrap(),
        json!([]),
        "re-enable checkpoint must skip existing source"
    );
    f.source("e\u{301}").await;
    assert_eq!(
        f.automation
            .run_once(PolicyKind::Detection, SID)
            .await
            .unwrap()
            .as_array()
            .unwrap()
            .len(),
        1
    );
    f.close().await;
}

#[tokio::test]
async fn original_manual_reconnect_or_project_receipt_is_never_recaptured_after_science() {
    for reason in ["manual capture", "worker reconnect", "project claim"] {
        let f = Fixture::new().await;
        f.automation
            .configure(
                PolicyKind::Detection,
                SID,
                json!({"enabled":true,"threshold_dbm":-60.0}),
            )
            .await
            .unwrap();
        f.source(reason).await;
        f.effects.block_science.store(true, Ordering::SeqCst);
        let client = f.automation.clone();
        let running =
            tokio::spawn(async move { client.run_once(PolicyKind::Detection, SID).await });
        f.effects.entered.notified().await;
        f.effects.revision.fetch_add(1, Ordering::SeqCst);
        f.effects.release.notify_one();
        let _ = running.await.unwrap();
        assert_eq!(f.effects.captures.load(Ordering::SeqCst), 1, "{reason}");
        assert!(
            f.session
                .list_candidates(SID, None)
                .await
                .unwrap()
                .as_array()
                .unwrap()
                .is_empty(),
            "{reason}"
        );
        f.close().await;
    }
}

#[tokio::test]
async fn stopped_or_manually_cancelled_investigation_is_not_resurrected_by_late_tune() {
    for stop in [false, true] {
        let f = Fixture::new().await;
        f.queued("job", 8).await;
        f.automation
            .configure(PolicyKind::Handoff, SID, json!({"enabled":true}))
            .await
            .unwrap();
        f.effects.block_tune.store(true, Ordering::SeqCst);
        let client = f.automation.clone();
        let running = tokio::spawn(async move { client.run_once(PolicyKind::Handoff, SID).await });
        f.effects.entered.notified().await;
        if stop {
            let coordinator = f.archive.domain_coordinator();
            let _guard = coordinator.lock().await;
            let mut owner = f.archive.get_session(SID).await.unwrap().unwrap();
            owner["state"] = json!("stopped");
            f.archive
                .stop_session(owner, "2026-11-11T12:00:01Z")
                .await
                .unwrap();
        } else {
            f.session.cancel_investigation(SID, "job").await.unwrap();
        }
        f.effects.release.notify_one();
        let _ = running.await.unwrap();
        assert_eq!(
            f.session.get_investigation(SID, "job").await.unwrap()["state"],
            "cancelled"
        );
        assert!(
            !f.archive
                .events(0, 100)
                .await
                .unwrap()
                .iter()
                .any(|e| e["event_type"] == "investigation.listening")
        );
        f.close().await;
    }
}

#[tokio::test]
async fn close_future_cancellation_still_owns_callback_cleanup_and_blocks_late_writes() {
    let f = Fixture::new().await;
    f.automation
        .configure(
            PolicyKind::Detection,
            SID,
            json!({"enabled":true,"threshold_dbm":-60.0}),
        )
        .await
        .unwrap();
    f.source("blocked").await;
    f.effects.block_science.store(true, Ordering::SeqCst);
    let client = f.automation.clone();
    let running = tokio::spawn(async move { client.run_once(PolicyKind::Detection, SID).await });
    f.effects.entered.notified().await;
    let client = f.automation.clone();
    let closing = tokio::spawn(async move { client.close().await });
    tokio::task::yield_now().await;
    assert!(!closing.is_finished(), "close must wait for owned callback");
    closing.abort();
    let _ = closing.await;
    assert_eq!(f.effects.active.load(Ordering::SeqCst), 1);
    f.effects.release.notify_one();
    let _ = running.await.unwrap();
    f.automation.close().await.unwrap();
    assert_eq!(f.effects.active.load(Ordering::SeqCst), 0);
    assert!(
        f.session
            .list_candidates(SID, None)
            .await
            .unwrap()
            .as_array()
            .unwrap()
            .is_empty()
    );
    assert!(
        f.automation
            .run_once(PolicyKind::Detection, SID)
            .await
            .is_err()
    );
    f.archive.close().await.unwrap();
}

#[tokio::test]
async fn checked_candidate_waiting_for_domain_lock_rejects_invalid_original_receipt() {
    let f = Fixture::new().await;
    let coordinator = f.archive.domain_coordinator();
    let guard = coordinator.lock().await;
    let receipt = f.effects.receipt();
    let session = f.session.clone();
    let running = tokio::spawn(async move {
        session.create_candidate_with_check(SID,json!({"id":"blocked-candidate","source_stream_id":STREAM,"interval":{"started_at":"2026-11-11T12:00:00Z","ended_at":"2026-11-11T12:00:00Z"},"center_frequency_hz":1,"level_unit":"dBm"}),receipt).await
    });
    tokio::task::yield_now().await;
    f.effects.revision.fetch_add(1, Ordering::SeqCst);
    drop(guard);
    assert_eq!(
        running.await.unwrap().unwrap_err().api_code,
        "stale_receipt"
    );
    assert!(
        f.archive
            .get_candidate("blocked-candidate")
            .await
            .unwrap()
            .is_none()
    );
    f.close().await;
}

#[tokio::test]
async fn eligible_start_rechecks_priority_busy_receiver_and_original_under_domain_lock() {
    let f = Fixture::new().await;
    f.queued("low", 1).await;
    f.queued("high", 9).await;
    let low = f
        .session
        .start_eligible_investigation_with_effects(
            SID,
            "low",
            f.effects.clone(),
            f.effects.receipt(),
            0,
            None,
        )
        .await
        .unwrap_err();
    assert_eq!(low.http_status, 400);
    assert_eq!(
        f.session.get_investigation(SID, "low").await.unwrap()["state"],
        "queued"
    );
    assert_eq!(
        f.session
            .start_eligible_investigation_with_effects(
                SID,
                "high",
                f.effects.clone(),
                f.effects.receipt(),
                0,
                None
            )
            .await
            .unwrap()["state"],
        "listening"
    );
    // A different receiver remains eligible while the first one is listening.
    f.session.create_candidate(SID,json!({"id":"candidate-independent","source_stream_id":STREAM,"interval":{"started_at":"2026-11-11T12:00:00Z","ended_at":"2026-11-11T12:00:00Z"},"center_frequency_hz":100100001,"level_unit":"dBm"})).await.unwrap();
    f.session
        .enqueue_investigation(
            SID,
            "candidate-independent",
            json!({"id":"independent","target_stream_id":"receiver/e\u{301}","priority":0}),
        )
        .await
        .unwrap();
    let coordinator = f.archive.domain_coordinator();
    let guard = coordinator.lock().await;
    let receipt = f.effects.receipt();
    let session = f.session.clone();
    let effects = f.effects.clone();
    let pending = tokio::spawn(async move {
        session
            .start_eligible_investigation_with_effects(
                SID,
                "independent",
                effects,
                receipt,
                0,
                None,
            )
            .await
    });
    tokio::task::yield_now().await;
    f.effects.revision.fetch_add(1, Ordering::SeqCst);
    drop(guard);
    assert_eq!(
        pending.await.unwrap().unwrap_err().api_code,
        "stale_receipt"
    );
    assert_eq!(
        f.session
            .get_investigation(SID, "independent")
            .await
            .unwrap()["state"],
        "queued"
    );
    assert_eq!(
        f.session
            .start_eligible_investigation_with_effects(
                SID,
                "independent",
                f.effects.clone(),
                f.effects.receipt(),
                0,
                None
            )
            .await
            .unwrap()["state"],
        "listening"
    );
    f.close().await;
}

#[tokio::test]
async fn delayed_receiver_does_not_hold_ingestion_or_another_policy_dispatch_slot() {
    let f = Fixture::new().await;
    f.queued("job", 8).await;
    f.automation
        .configure(PolicyKind::Handoff, SID, json!({"enabled":true}))
        .await
        .unwrap();
    f.automation
        .configure(
            PolicyKind::Detection,
            SID,
            json!({"enabled":true,"threshold_dbm":-60.0}),
        )
        .await
        .unwrap();
    f.effects.block_tune.store(true, Ordering::SeqCst);
    let client = f.automation.clone();
    let handoff = tokio::spawn(async move { client.run_once(PolicyKind::Handoff, SID).await });
    f.effects.entered.notified().await;
    tokio::time::timeout(Duration::from_secs(2), f.source("during-tune"))
        .await
        .expect("receiver delay held ingestion");
    // The existing candidate is a protected duplicate. Detection still runs.
    assert_eq!(
        tokio::time::timeout(
            Duration::from_secs(2),
            f.automation.run_once(PolicyKind::Detection, SID)
        )
        .await
        .expect("receiver delay held detector")
        .unwrap(),
        json!([])
    );
    assert_eq!(
        f.automation
            .run_once(PolicyKind::Handoff, SID)
            .await
            .unwrap_err()
            .api_code,
        "automation_busy"
    );
    f.effects.release.notify_one();
    handoff.await.unwrap().unwrap();
    f.close().await;
}

#[tokio::test]
async fn background_requires_opt_in_and_session_capacity_rejection_has_no_audit_or_work() {
    let f = Fixture::new().await;
    f.automation.close().await.unwrap();
    f.source("historical").await;
    let automation = AutomationService::new(
        f.archive.clone(),
        f.session.clone(),
        f.effects.clone(),
        AutomationConfig {
            max_sessions: 1,
            max_in_flight: 1,
            max_recent: 1000,
            ..AutomationConfig::default()
        },
    )
    .unwrap();
    assert_eq!(
        automation
            .run_once(PolicyKind::Detection, SID)
            .await
            .unwrap(),
        json!([])
    );
    automation
        .configure(
            PolicyKind::Detection,
            SID,
            json!({"enabled":true,"threshold_dbm":-60.0,"interval_seconds":0.1}),
        )
        .await
        .unwrap();
    f.source("new").await;
    tokio::time::timeout(Duration::from_secs(2),async {
        loop {
            if automation.status(PolicyKind::Detection,SID).await.unwrap()["runtime"]["sweeps_processed"]==1 {break;}
            tokio::task::yield_now().await;
        }
    }).await.expect("enabled scheduler did not process the new source");
    assert_eq!(
        f.session
            .list_candidates(SID, None)
            .await
            .unwrap()
            .as_array()
            .unwrap()
            .len(),
        1
    );
    automation
        .configure(PolicyKind::Detection, SID, json!({"enabled":false}))
        .await
        .unwrap();
    f.source("disabled").await;
    tokio::time::sleep(Duration::from_millis(150)).await;
    assert_eq!(
        automation.status(PolicyKind::Detection, SID).await.unwrap()["runtime"]["sweeps_processed"],
        1
    );
    let mut second = f.archive.get_session(SID).await.unwrap().unwrap();
    second["id"] = json!("session-e\u{301}");
    for stream in second["streams"].as_array_mut().unwrap() {
        stream["session_id"] = json!("session-e\u{301}");
        stream["id"] = json!(format!("second-{}", stream["id"].as_str().unwrap()));
    }
    f.archive.create_session(second).await.unwrap();
    let before = f.archive.latest_events(1).await.unwrap()[0]["sequence"].clone();
    assert_eq!(
        automation
            .configure(
                PolicyKind::Detection,
                "session-e\u{301}",
                json!({"enabled":true})
            )
            .await
            .unwrap_err()
            .api_code,
        "automation_capacity"
    );
    assert_eq!(
        f.archive.latest_events(1).await.unwrap()[0]["sequence"],
        before
    );
    automation.close().await.unwrap();
    f.close().await;
}

use chrono::{DateTime, Utc};
use ori_archive::{Archive, ArchiveConfig};
use ori_session::{
    SessionEffects, SessionError, SessionFuture, SessionService, TuneCompletionCheck, TuneReceipt,
    TuneRequest,
};
use serde_json::{Value, json};
use std::sync::{
    Arc,
    atomic::{AtomicBool, AtomicUsize, Ordering},
};
use tokio::sync::Notify;

struct Completion(Arc<AtomicBool>);
impl TuneCompletionCheck for Completion {
    fn revalidate(&self) -> Result<(), SessionError> {
        if self.0.load(Ordering::SeqCst) {
            Ok(())
        } else {
            Err(SessionError {
                http_status: 409,
                api_code: "stale_tune".into(),
                message: "Original tune generation is stale".into(),
            })
        }
    }
}
struct Blocking {
    entered: Notify,
    release: Notify,
    valid: Arc<AtomicBool>,
    fail: bool,
    calls: AtomicUsize,
}
impl Blocking {
    fn new(fail: bool) -> Arc<Self> {
        Arc::new(Self {
            entered: Notify::new(),
            release: Notify::new(),
            valid: Arc::new(AtomicBool::new(true)),
            fail,
            calls: AtomicUsize::new(0),
        })
    }
}
impl SessionEffects for Blocking {
    fn tune(&self, _request: TuneRequest) -> SessionFuture<'_, TuneReceipt> {
        Box::pin(async move {
            self.calls.fetch_add(1, Ordering::SeqCst);
            self.entered.notify_one();
            self.release.notified().await;
            if self.fail {
                return Err(SessionError {
                    http_status: 503,
                    api_code: "effect_failed".into(),
                    message: "frequency applied; mode failed".into(),
                });
            }
            Ok(TuneReceipt::new(Arc::new(Completion(self.valid.clone()))))
        })
    }
    fn validate_reference(&self, _session: Value, _sweep: Value) -> SessionFuture<'_, ()> {
        Box::pin(async {
            Err(SessionError {
                http_status: 503,
                api_code: "unavailable".into(),
                message: "reference callback unavailable in receiver fixture".into(),
            })
        })
    }
}

async fn fixture(effects: Arc<Blocking>) -> (tempfile::TempDir, Archive, Arc<SessionService>) {
    let directory = tempfile::tempdir().unwrap();
    let archive =
        Archive::open(ArchiveConfig::new(directory.path().join("archive.sqlite"))).unwrap();
    let source: Value = serde_json::from_str(include_str!(
        "../../../contracts/fixtures/session-domain-v1.json"
    ))
    .unwrap();
    archive
        .create_session(source["cases"][0]["seed"]["sessions"][0].clone())
        .await
        .unwrap();
    let service = SessionService::with_sources(
        archive.clone(),
        effects,
        Arc::new(|| {
            "2026-11-11T12:00:00.123456Z"
                .parse::<DateTime<Utc>>()
                .unwrap()
        }),
        Arc::new(|| "generated".into()),
    );
    service.create_candidate("session-α", json!({"id":"candidate","source_stream_id":"spectrum/α",
        "interval":{"started_at":"2026-11-11T12:00:00.123456Z","ended_at":"2026-11-11T12:00:00.123456Z"},
        "center_frequency_hz":100000003,"level_unit":"dBm","peak_level":-42.5})).await.unwrap();
    service
        .enqueue_investigation(
            "session-α",
            "candidate",
            json!({"id":"job","target_stream_id":"receiver+α","mode":"FM"}),
        )
        .await
        .unwrap();
    (directory, archive, service)
}

#[tokio::test]
async fn cancellation_wins_over_both_late_success_and_partial_tune_failure() {
    for fail in [false, true] {
        let effects = Blocking::new(fail);
        let (_directory, archive, service) = fixture(effects.clone()).await;
        let starter = service.clone();
        let running =
            tokio::spawn(async move { starter.start_investigation("session-α", "job").await });
        effects.entered.notified().await;
        let cancelled = tokio::time::timeout(
            std::time::Duration::from_secs(2),
            service.cancel_investigation("session-α", "job"),
        )
        .await
        .expect("domain coordinator must not be held during tune")
        .unwrap();
        assert_eq!(cancelled["state"], "cancelled");
        effects.release.notify_one();
        assert_eq!(running.await.unwrap().unwrap()["state"], "cancelled");
        assert_eq!(
            service
                .get_candidate("session-α", "candidate")
                .await
                .unwrap()["state"],
            "queued"
        );
        let events = archive.events(0, 100).await.unwrap();
        assert!(
            !events
                .iter()
                .any(|event| event["event_type"] == "investigation.listening")
        );
        archive.close().await.unwrap();
    }
}

#[tokio::test]
async fn core_stop_on_the_same_archive_coordinator_cannot_be_resurrected() {
    let effects = Blocking::new(false);
    let (_directory, archive, service) = fixture(effects.clone()).await;
    let starter = service.clone();
    let running =
        tokio::spawn(async move { starter.start_investigation("session-α", "job").await });
    effects.entered.notified().await;
    let coordinator = archive.domain_coordinator();
    let guard = tokio::time::timeout(std::time::Duration::from_secs(2), coordinator.lock())
        .await
        .unwrap();
    let mut session = archive.get_session("session-α").await.unwrap().unwrap();
    session["state"] = json!("stopped");
    archive
        .stop_session(session, "2026-11-11T12:00:01.123456Z")
        .await
        .unwrap();
    drop(guard);
    effects.release.notify_one();
    assert_eq!(running.await.unwrap().unwrap()["state"], "cancelled");
    assert_eq!(
        archive.get_session("session-α").await.unwrap().unwrap()["state"],
        "stopped"
    );
    archive.close().await.unwrap();
}

#[tokio::test]
async fn stale_original_completion_receipt_fails_instead_of_entering_listening() {
    let effects = Blocking::new(false);
    let (_directory, archive, service) = fixture(effects.clone()).await;
    let starter = service.clone();
    let running =
        tokio::spawn(async move { starter.start_investigation("session-α", "job").await });
    effects.entered.notified().await;
    effects.valid.store(false, Ordering::SeqCst);
    effects.release.notify_one();
    let value = running.await.unwrap().unwrap();
    assert_eq!(value["state"], "failed");
    assert_eq!(value["error"], "Original tune generation is stale");
    assert_eq!(
        service
            .get_candidate("session-α", "candidate")
            .await
            .unwrap()["state"],
        "queued"
    );
    archive.close().await.unwrap();
}

#[tokio::test]
async fn per_job_effects_keep_original_authorization_instead_of_refreshing_defaults() {
    let default = Blocking::new(false);
    let original = Blocking::new(false);
    let (_directory, archive, service) = fixture(default.clone()).await;
    let starter = service.clone();
    let effect = original.clone();
    let running = tokio::spawn(async move {
        starter
            .start_investigation_with_effects("session-α", "job", effect)
            .await
    });
    original.entered.notified().await;
    original.valid.store(false, Ordering::SeqCst);
    original.release.notify_one();
    assert_eq!(running.await.unwrap().unwrap()["state"], "failed");
    assert_eq!(original.calls.load(Ordering::SeqCst), 1);
    assert_eq!(default.calls.load(Ordering::SeqCst), 0);
    archive.close().await.unwrap();
}

struct CommitCheck {
    coordinator: Arc<tokio::sync::Mutex<()>>,
    valid: Arc<AtomicBool>,
}
impl TuneCompletionCheck for CommitCheck {
    fn revalidate(&self) -> Result<(), SessionError> {
        if self.coordinator.try_lock().is_ok() {
            return Err(SessionError::new(
                500,
                "check_outside_coordinator",
                "completion check ran outside its commit lock",
            ));
        }
        if !self.valid.load(Ordering::SeqCst) {
            return Err(SessionError::new(
                409,
                "stale_permit",
                "original authorization was revoked",
            ));
        }
        Ok(())
    }
}
async fn listening_fixture() -> (tempfile::TempDir, Archive, Arc<SessionService>) {
    let effects = Blocking::new(false);
    let fixture = fixture(effects.clone()).await;
    let starter = fixture.2.clone();
    let running =
        tokio::spawn(async move { starter.start_investigation("session-α", "job").await });
    effects.entered.notified().await;
    effects.release.notify_one();
    assert_eq!(running.await.unwrap().unwrap()["state"], "listening");
    let source: Value = serde_json::from_str(include_str!(
        "../../../contracts/fixtures/session-domain-v1.json"
    ))
    .unwrap();
    fixture
        .1
        .append_observations(vec![source["cases"][0]["seed"]["observations"][3].clone()])
        .await
        .unwrap();
    fixture
}

#[tokio::test]
async fn completion_original_check_and_evidence_commit_share_the_coordinator() {
    let (_directory, archive, service) = listening_fixture().await;
    let check = Arc::new(CommitCheck {
        coordinator: archive.domain_coordinator(),
        valid: Arc::new(AtomicBool::new(true)),
    });
    let value = service
        .complete_investigation_with_check("session-α", "job", check)
        .await
        .unwrap();
    assert_eq!(value["state"], "completed");
    assert_eq!(value["result_observation_ids"].as_array().unwrap().len(), 1);
    archive.close().await.unwrap();
}

#[tokio::test]
async fn original_permit_revoked_before_completion_lock_prevents_evidence_commit() {
    let (_directory, archive, service) = listening_fixture().await;
    let coordinator = archive.domain_coordinator();
    let guard = coordinator.lock().await;
    let valid = Arc::new(AtomicBool::new(true));
    let check = Arc::new(CommitCheck {
        coordinator: coordinator.clone(),
        valid: valid.clone(),
    });
    let starter = service.clone();
    let completing = tokio::spawn(async move {
        starter
            .complete_investigation_with_check("session-α", "job", check)
            .await
    });
    tokio::task::yield_now().await;
    valid.store(false, Ordering::SeqCst);
    drop(guard);
    assert_eq!(
        completing.await.unwrap().unwrap_err().api_code,
        "stale_permit"
    );
    assert_eq!(
        service.get_investigation("session-α", "job").await.unwrap()["state"],
        "listening"
    );
    assert_eq!(
        service
            .get_candidate("session-α", "candidate")
            .await
            .unwrap()["state"],
        "investigating"
    );
    archive.close().await.unwrap();
}

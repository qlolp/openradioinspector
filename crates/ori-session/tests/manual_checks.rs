use chrono::{DateTime, Utc};
use ori_archive::{Archive, ArchiveConfig};
use ori_session::{
    SessionEffects, SessionError, SessionFuture, SessionService, TuneCompletionCheck, TuneReceipt,
    TuneRequest,
};
use serde_json::{Value, json};
use std::{
    future::Future,
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
    task::Poll,
};
use tokio::sync::{Mutex, Notify, oneshot};

struct Receipt(Arc<AtomicBool>);
impl TuneCompletionCheck for Receipt {
    fn revalidate(&self) -> Result<(), SessionError> {
        if self.0.load(Ordering::SeqCst) {
            Ok(())
        } else {
            Err(SessionError::new(
                409,
                "stale_tune",
                "Original tune receipt is stale",
            ))
        }
    }
}
struct Effects {
    block: bool,
    entered: Notify,
    release: Notify,
    returned: Notify,
    receipt_live: Arc<AtomicBool>,
    calls: AtomicUsize,
}
impl Effects {
    fn new(block: bool) -> Arc<Self> {
        Arc::new(Self {
            block,
            entered: Notify::new(),
            release: Notify::new(),
            returned: Notify::new(),
            receipt_live: Arc::new(AtomicBool::new(true)),
            calls: AtomicUsize::new(0),
        })
    }
}
impl SessionEffects for Effects {
    fn tune(&self, _request: TuneRequest) -> SessionFuture<'_, TuneReceipt> {
        Box::pin(async move {
            self.calls.fetch_add(1, Ordering::SeqCst);
            self.entered.notify_one();
            if self.block {
                self.release.notified().await;
            }
            self.returned.notify_one();
            Ok(TuneReceipt::new(Arc::new(Receipt(
                self.receipt_live.clone(),
            ))))
        })
    }
    fn validate_reference(&self, _session: Value, _sweep: Value) -> SessionFuture<'_, ()> {
        Box::pin(async {
            Err(SessionError::new(
                503,
                "unused_fixture",
                "No research callback",
            ))
        })
    }
}
struct Original {
    coordinator: Arc<Mutex<()>>,
    live: Arc<AtomicBool>,
    drops: Arc<AtomicUsize>,
}
impl TuneCompletionCheck for Original {
    fn revalidate(&self) -> Result<(), SessionError> {
        if self.coordinator.try_lock().is_ok() {
            return Err(SessionError::new(
                500,
                "outside_coordinator",
                "Original check is outside its commit coordinator",
            ));
        }
        if !self.live.load(Ordering::SeqCst) {
            return Err(SessionError::new(
                401,
                "original_revoked",
                "Original manual authorization was revoked",
            ));
        }
        Ok(())
    }
}
impl Drop for Original {
    fn drop(&mut self) {
        self.drops.fetch_add(1, Ordering::SeqCst);
    }
}
fn original(archive: &Archive) -> Arc<Original> {
    Arc::new(Original {
        coordinator: archive.domain_coordinator(),
        live: Arc::new(AtomicBool::new(true)),
        drops: Arc::new(AtomicUsize::new(0)),
    })
}
async fn fixture() -> (
    tempfile::TempDir,
    Archive,
    Arc<SessionService>,
    Arc<Effects>,
) {
    let root = tempfile::tempdir().unwrap();
    let archive = Archive::open(ArchiveConfig::new(root.path().join("source.sqlite"))).unwrap();
    let source: Value = serde_json::from_str(include_str!(
        "../../../contracts/fixtures/session-domain-v1.json"
    ))
    .unwrap();
    archive
        .create_session(source["cases"][0]["seed"]["sessions"][0].clone())
        .await
        .unwrap();
    let defaults = Effects::new(false);
    let service = SessionService::with_sources(
        archive.clone(),
        defaults.clone(),
        Arc::new(|| {
            "2026-11-11T12:00:00.123456Z"
                .parse::<DateTime<Utc>>()
                .unwrap()
        }),
        Arc::new(|| "unused-generated".into()),
    );
    enqueue(&service, "low", 10).await;
    (root, archive, service, defaults)
}
async fn enqueue(service: &SessionService, suffix: &str, priority: u64) {
    service.create_candidate("session-α", json!({"id":format!("candidate-{suffix}"),"source_stream_id":"spectrum/α",
        "interval":{"started_at":"2026-11-11T12:00:00.123456Z","ended_at":"2026-11-11T12:00:00.123456Z"},
        "center_frequency_hz":100000003,"level_unit":"dBm","peak_level":-0.0})).await.unwrap();
    service.enqueue_investigation("session-α", &format!("candidate-{suffix}"),
        json!({"id":format!("job-{suffix}"),"target_stream_id":"receiver+α","mode":"FM","priority":priority}))
        .await.unwrap();
}
async fn reached_pending(future: &mut (impl Future + Unpin)) {
    std::future::poll_fn(
        |context| match std::pin::Pin::new(&mut *future).poll(context) {
            Poll::Pending => Poll::Ready(()),
            Poll::Ready(_) => panic!("operation did not wait for the held coordinator"),
        },
    )
    .await;
}
fn no_listening(events: &[Value]) {
    assert!(
        !events
            .iter()
            .any(|event| event["event_type"] == "investigation.listening")
    );
}

#[tokio::test]
async fn original_revoked_while_manual_start_waits_for_coordinator_prevents_any_commit_or_tune() {
    for next in [false, true] {
        let (_root, archive, service, defaults) = fixture().await;
        let effect = Effects::new(false);
        let check = original(&archive);
        let before_job = service
            .get_investigation("session-α", "job-low")
            .await
            .unwrap();
        let before_candidate = service
            .get_candidate("session-α", "candidate-low")
            .await
            .unwrap();
        let before_events = archive.events(0, 100).await.unwrap();
        let coordinator = archive.domain_coordinator();
        let held = coordinator.lock().await;
        let mut operation = Box::pin(async {
            if next {
                service
                    .start_next_investigation_with_effects_and_check(
                        "session-α",
                        "job-low",
                        effect.clone(),
                        check.clone(),
                    )
                    .await
            } else {
                service
                    .start_investigation_with_effects_and_check(
                        "session-α",
                        "job-low",
                        effect.clone(),
                        check.clone(),
                    )
                    .await
            }
        });
        reached_pending(&mut operation).await;
        check.live.store(false, Ordering::SeqCst);
        drop(held);
        let error = operation
            .await
            .expect_err("revoked original must not commit tuning or execute effects");
        assert_eq!(error.api_code, "original_revoked");
        assert_eq!(
            service
                .get_investigation("session-α", "job-low")
                .await
                .unwrap(),
            before_job
        );
        assert_eq!(
            service
                .get_candidate("session-α", "candidate-low")
                .await
                .unwrap(),
            before_candidate
        );
        assert_eq!(archive.events(0, 100).await.unwrap(), before_events);
        assert_eq!(effect.calls.load(Ordering::SeqCst), 0);
        assert_eq!(defaults.calls.load(Ordering::SeqCst), 0);
        archive.close().await.unwrap();
    }
}

#[tokio::test]
async fn revocation_while_post_tune_commit_is_queued_terminalizes_failed_not_listening() {
    let (_root, archive, service, defaults) = fixture().await;
    let effect = Effects::new(true);
    let check = original(&archive);
    let starter = service.clone();
    let scoped = check.clone();
    let tuned = effect.clone();
    let operation = tokio::spawn(async move {
        starter
            .start_investigation_with_effects_and_check("session-α", "job-low", tuned, scoped)
            .await
    });
    effect.entered.notified().await;
    assert_eq!(
        service
            .get_investigation("session-α", "job-low")
            .await
            .unwrap()["state"],
        "tuning"
    );
    let coordinator = archive.domain_coordinator();
    let held = coordinator.lock().await;
    effect.release.notify_one();
    effect.returned.notified().await;
    assert!(!operation.is_finished());
    check.live.store(false, Ordering::SeqCst);
    drop(held);
    let value = operation.await.unwrap().unwrap();
    assert_eq!(value["state"], "failed");
    assert_eq!(value["error"], "Original manual authorization was revoked");
    assert_eq!(
        service
            .get_candidate("session-α", "candidate-low")
            .await
            .unwrap()["state"],
        "queued"
    );
    no_listening(&archive.events(0, 100).await.unwrap());
    assert_eq!(effect.calls.load(Ordering::SeqCst), 1);
    assert_eq!(defaults.calls.load(Ordering::SeqCst), 0);
    archive.close().await.unwrap();
}

#[tokio::test]
async fn owned_start_retains_the_same_original_after_result_observer_drop() {
    let (_root, archive, service, defaults) = fixture().await;
    let effect = Effects::new(true);
    let check = original(&archive);
    let live = check.live.clone();
    let drops = check.drops.clone();
    let weak = Arc::downgrade(&check);
    let (send, observer) = oneshot::channel();
    let starter = service.clone();
    let tuned = effect.clone();
    let owned = tokio::spawn(async move {
        let result = starter
            .start_investigation_with_effects_and_check("session-α", "job-low", tuned, check)
            .await;
        let _ = send.send(result.clone());
        result
    });
    effect.entered.notified().await;
    drop(observer);
    assert!(
        weak.upgrade().is_some(),
        "the original check must remain owned until completion"
    );
    live.store(false, Ordering::SeqCst);
    effect.release.notify_one();
    let value = owned.await.unwrap().unwrap();
    assert_eq!(value["state"], "failed");
    assert_eq!(value["error"], "Original manual authorization was revoked");
    assert!(weak.upgrade().is_none());
    assert_eq!(drops.load(Ordering::SeqCst), 1);
    assert_eq!(defaults.calls.load(Ordering::SeqCst), 0);
    no_listening(&archive.events(0, 100).await.unwrap());
    archive.close().await.unwrap();
}

#[tokio::test]
async fn a_live_original_cannot_replace_the_returned_stale_tune_receipt() {
    let (_root, archive, service, defaults) = fixture().await;
    let effect = Effects::new(false);
    effect.receipt_live.store(false, Ordering::SeqCst);
    let value = service
        .start_investigation_with_effects_and_check(
            "session-α",
            "job-low",
            effect,
            original(&archive),
        )
        .await
        .unwrap();
    assert_eq!(value["state"], "failed");
    assert_eq!(value["error"], "Original tune receipt is stale");
    assert_eq!(defaults.calls.load(Ordering::SeqCst), 0);
    no_listening(&archive.events(0, 100).await.unwrap());
    archive.close().await.unwrap();
}

#[tokio::test]
async fn checked_manual_target_and_confirmed_next_keep_their_distinct_selection_rules() {
    for next in [false, true] {
        let (_root, archive, service, defaults) = fixture().await;
        enqueue(&service, "high", 90).await;
        assert_eq!(
            service.next_investigation("session-α").await.unwrap()["id"],
            "job-high"
        );
        let effect = Effects::new(false);
        if next {
            let before = archive.events(0, 100).await.unwrap();
            assert!(
                service
                    .start_next_investigation_with_effects_and_check(
                        "session-α",
                        "job-low",
                        effect.clone(),
                        original(&archive)
                    )
                    .await
                    .is_err()
            );
            assert_eq!(effect.calls.load(Ordering::SeqCst), 0);
            assert_eq!(archive.events(0, 100).await.unwrap(), before);
            let value = service
                .start_next_investigation_with_effects_and_check(
                    "session-α",
                    "job-high",
                    effect.clone(),
                    original(&archive),
                )
                .await
                .unwrap();
            assert_eq!(value["state"], "listening");
        } else {
            let value = service
                .start_investigation_with_effects_and_check(
                    "session-α",
                    "job-low",
                    effect.clone(),
                    original(&archive),
                )
                .await
                .unwrap();
            assert_eq!(
                value["state"], "listening",
                "manual start does not adopt automatic first-eligible selection"
            );
        }
        assert_eq!(effect.calls.load(Ordering::SeqCst), 1);
        assert_eq!(defaults.calls.load(Ordering::SeqCst), 0);
        archive.close().await.unwrap();
    }
}

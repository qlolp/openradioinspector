mod common;
use common::*;
use ori_guard::{ControlTarget, GuardService, IdKind, IdSource, InstrumentControl, WorkerStatus};
use serde_json::{Value, json};
use std::sync::{
    Arc,
    atomic::{AtomicU64, Ordering},
};

#[tokio::test]
async fn revoked_coverage_wait_releases_before_the_actual_project_lock() {
    let h = Harness::new().await;
    let (id, _, target, analyzer) = manual(&h, "analyzer").await;
    let receiver = h
        .service
        .manual_acquire(&id, json!({"role":"receiver"}))
        .await
        .unwrap();
    let grant = h
        .service
        .capture_instrument_control(&target, analyzer["token"].as_str())
        .unwrap();
    let gate = TuneGate::new();
    *h.control.tune_gate.lock().unwrap() = Some(gate.clone());
    let service = h.service.clone();
    let project = id.clone();
    let tuning = tokio::spawn(async move {
        service
            .manual_tune(
                &project,
                json!({"token":receiver["token"],"frequency_hz":145500000,"mode":"FM"}),
            )
            .await
    });
    gate.entered.notified().await;
    // The public receiver tune owns this actual project's lifecycle lock.
    let confirming_service = h.service.clone();
    let confirming = confirming_service.confirm_instrument_coverage(&grant);
    tokio::pin!(confirming);
    assert!(
        std::future::poll_fn(|cx| std::task::Poll::Ready(
            std::future::Future::poll(confirming.as_mut(), cx).is_pending()
        ))
        .await
    );
    let disarming_service = h.service.clone();
    let disarming = disarming_service.disarm(&id, json!({}));
    tokio::pin!(disarming);
    assert!(
        std::future::poll_fn(|cx| std::task::Poll::Ready(
            std::future::Future::poll(disarming.as_mut(), cx).is_pending()
        ))
        .await
    );
    assert!(
        grant.check().is_err(),
        "Actual disarm must revoke the same original grant"
    );
    assert!(
        !tuning.is_finished(),
        "The project lock must remain held for this check"
    );
    let raced = std::future::poll_fn(|cx| {
        std::task::Poll::Ready(match std::future::Future::poll(confirming.as_mut(), cx) {
            std::task::Poll::Ready(result) => Some(result),
            std::task::Poll::Pending => None,
        })
    })
    .await;
    let released_before_lock = raced.is_some();
    gate.release.notify_one();
    assert!(tuning.await.unwrap().is_err());
    let confirmed = match raced {
        Some(result) => result,
        None => confirming.await,
    };
    disarming.await.unwrap();
    *h.control.tune_gate.lock().unwrap() = None;
    let coverage = h.service.project(&id).await.unwrap()["coverage"].clone();
    h.close().await;
    assert!(confirmed.is_err());
    assert_eq!(coverage, "stopped");
    assert!(
        released_before_lock,
        "Revoked coverage must finish while the project lifecycle is still held"
    );
}

struct ClaimIds {
    records: AtomicU64,
    streams: AtomicU64,
}
impl IdSource for ClaimIds {
    fn next_id(&self, kind: IdKind) -> String {
        match kind {
            IdKind::Session => "explicit-claim-session".into(),
            IdKind::Stream => format!(
                "explicit-claim-stream-{}",
                self.streams.fetch_add(1, Ordering::SeqCst)
            ),
            _ => format!("{:032x}", self.records.fetch_add(1, Ordering::SeqCst) + 1),
        }
    }
}
async fn predictable_claim() -> Harness {
    let mut harness = Harness::new().await;
    harness.service = GuardService::with_sources(
        harness.archive.clone(),
        harness.control.clone(),
        harness.sources.clone(),
        Arc::new(ClaimIds {
            records: AtomicU64::new(0),
            streams: AtomicU64::new(0),
        }),
    );
    *harness.control.service.lock().unwrap() = Arc::downgrade(&harness.service);
    harness
}

#[tokio::test]
async fn original_unclaimed_receipt_stays_revoked_after_actual_claim_and_return() {
    let h = predictable_claim().await;
    let id = h.prepared().await;
    // The explicit IdSource owns these identities before the actual arm/claim.
    let target = ControlTarget {
        session_id: "explicit-claim-session".into(),
        stream_id: "explicit-claim-stream-0".into(),
    };
    let original = h.service.capture_instrument_control(&target, None).unwrap();
    original.check().unwrap();
    let armed = h.service.arm(&id, json!({})).await.unwrap();
    assert_eq!(armed["active_session_id"], target.session_id);
    assert_eq!(armed["analyzer_stream_id"], target.stream_id);
    h.service.disarm(&id, json!({})).await.unwrap();
    // Its FIRST check since the claim happens after return. A current None
    // policy query would resurrect; the original absence receipt must not.
    assert!(original.check().is_err());
    h.service
        .capture_instrument_control(&target, None)
        .unwrap()
        .check()
        .unwrap();
    assert!(original.check().is_err());
    h.close().await;
}

#[tokio::test]
async fn claiming_one_stream_does_not_revoke_an_unrelated_stream_receipt() {
    let h = predictable_claim().await;
    let id = h.prepared().await;
    let target = ControlTarget {
        session_id: "explicit-claim-session".into(),
        stream_id: "unrelated-stream".into(),
    };
    let before = h.control.calls.lock().unwrap().clone();
    let grant = h.service.capture_instrument_control(&target, None).unwrap();
    for _ in 0..20 {
        grant.check().unwrap();
    }
    assert_eq!(
        *h.control.calls.lock().unwrap(),
        before,
        "Capture/check must perform no control I/O"
    );
    h.service.arm(&id, json!({})).await.unwrap();
    grant.check().unwrap();
    h.service.disarm(&id, json!({})).await.unwrap();
    grant.check().unwrap();
    h.service.close().await.unwrap();
    assert!(grant.check().is_err());
    h.archive.close().await.unwrap();
}

#[tokio::test]
async fn preparation_claim_also_permanently_revokes_the_original_absence_receipt() {
    let h = predictable_claim().await;
    let id = h.prepared().await;
    let target = ControlTarget {
        session_id: "explicit-claim-session".into(),
        stream_id: "explicit-claim-stream-0".into(),
    };
    let original = h.service.capture_instrument_control(&target, None).unwrap();
    h.service.start_preparation(&id, json!({})).await.unwrap();
    h.service.stop_preparation(&id, json!({})).await.unwrap();
    assert!(original.check().is_err());
    h.service
        .capture_instrument_control(&target, None)
        .unwrap()
        .check()
        .unwrap();
    h.close().await;
}

#[tokio::test]
async fn paused_coverage_waits_for_the_actual_analyzer_stop_receipt() {
    let h = Harness::new().await;
    let id = h.prepared().await;
    h.service.arm(&id, json!({})).await.unwrap();
    let gate = TuneGate::new();
    *h.control.stream_stop_gate.lock().unwrap() = Some(gate.clone());
    let service = h.service.clone();
    let project = id.clone();
    let acquire = tokio::spawn(async move {
        service
            .manual_acquire(&project, json!({"role":"analyzer"}))
            .await
    });
    gate.entered.notified().await;
    let before_ack = h.service.project(&id).await.unwrap()["coverage"].clone();
    gate.release.notify_one();
    acquire.await.unwrap().unwrap();
    let after_ack = h.service.project(&id).await.unwrap()["coverage"].clone();
    *h.control.stream_stop_gate.lock().unwrap() = None;
    h.close().await;
    assert_ne!(before_ack, "paused");
    assert_eq!(after_ack, "paused");
}

#[tokio::test]
async fn paused_coverage_is_never_claimed_from_an_unconfirmed_stop() {
    let h = Harness::new().await;
    let id = h.prepared().await;
    h.service.arm(&id, json!({})).await.unwrap();
    *h.control.stream_stop_acknowledged.lock().unwrap() = false;
    let result = h
        .service
        .manual_acquire(&id, json!({"role":"analyzer"}))
        .await;
    let coverage = h.service.project(&id).await.unwrap()["coverage"].clone();
    h.close().await;
    assert!(result.is_err());
    assert_ne!(coverage, "paused");
}

async fn manual(h: &Harness, role: &str) -> (String, Value, ControlTarget, Value) {
    let id = h.prepared().await;
    let armed = h.service.arm(&id, json!({})).await.unwrap();
    let lease = h
        .service
        .manual_acquire(&id, json!({"role":role}))
        .await
        .unwrap();
    let target = ControlTarget {
        session_id: armed["active_session_id"].as_str().unwrap().into(),
        stream_id: armed[format!("{role}_stream_id")].as_str().unwrap().into(),
    };
    (id, armed, target, lease)
}
fn worker(h: &Harness, target: &ControlTarget) -> WorkerStatus {
    h.control
        .workers
        .lock()
        .unwrap()
        .iter()
        .find(|worker| {
            worker.session_id == target.session_id && worker.stream_id == target.stream_id
        })
        .unwrap()
        .clone()
}
fn explicit_connected_worker(h: &Harness, target: &ControlTarget, instance: &str) -> WorkerStatus {
    let mut connected = worker(h, target);
    connected.alive = true;
    connected.state = "running".into();
    connected.worker_instance = Some(instance.into());
    let mut workers = h.control.workers.lock().unwrap();
    workers.retain(|worker| {
        worker.session_id != target.session_id || worker.stream_id != target.stream_id
    });
    workers.push(connected.clone());
    connected
}

#[tokio::test]
async fn manual_receiver_grant_renews_without_refresh_and_never_revives_after_return() {
    let h = Harness::new().await;
    let (id, _, target, lease) = manual(&h, "receiver").await;
    let token = lease["token"].as_str().unwrap();
    let grant = h
        .service
        .capture_instrument_control(&target, Some(token))
        .unwrap();
    assert_eq!(grant.target(), &target);
    assert_eq!(grant.manual_role(), Some("receiver"));
    assert_eq!(
        grant.expected_worker_instance(),
        worker(&h, &target).worker_instance.as_deref()
    );
    h.sources.advance(20);
    h.service
        .manual_renew(&id, json!({"token":token}))
        .await
        .unwrap();
    h.sources.advance(15);
    grant.check().unwrap();
    h.service
        .manual_return(&id, json!({"token":token}))
        .await
        .unwrap();
    assert!(grant.check().is_err());
    let next = h
        .service
        .manual_acquire(&id, json!({"role":"receiver"}))
        .await
        .unwrap();
    let new_grant = h
        .service
        .capture_instrument_control(&target, next["token"].as_str())
        .unwrap();
    new_grant.check().unwrap();
    assert!(grant.check().is_err());
    h.sources.advance(31);
    assert!(new_grant.check().is_err());
    assert!(
        h.service
            .manual_renew(&id, json!({"token":next["token"]}))
            .await
            .is_err()
    );
    h.close().await;
}

#[tokio::test]
async fn analyzer_handoff_retains_original_stopped_context_and_binds_once() {
    let h = Harness::new().await;
    let (id, _, target, lease) = manual(&h, "analyzer").await;
    let token = lease["token"].as_str().unwrap();
    let stopped = worker(&h, &target);
    let grant = h
        .service
        .capture_instrument_control(&target, Some(token))
        .unwrap();
    let clone = grant.clone();
    assert_eq!(grant.manual_role(), Some("analyzer"));
    assert_eq!(grant.expected_worker_instance(), None);
    assert_eq!(
        grant.stopped_worker_instance(),
        stopped.worker_instance.as_deref()
    );
    assert!(h.service.bind_manual_worker(&grant, &stopped).is_err());
    let connected = explicit_connected_worker(&h, &target, "explicit-core-owned-new-instance");
    let calls = h.control.calls.lock().unwrap().clone();
    let mut foreign = connected.clone();
    foreign.stream_id = "foreign".into();
    assert!(h.service.bind_manual_worker(&grant, &foreign).is_err());
    h.service.bind_manual_worker(&grant, &connected).unwrap();
    assert_eq!(
        clone.expected_worker_instance(),
        Some("explicit-core-owned-new-instance")
    );
    assert!(h.service.bind_manual_worker(&clone, &connected).is_err());
    h.service
        .manual_renew(&id, json!({"token":token}))
        .await
        .unwrap();
    grant.check().unwrap();
    h.service.confirm_instrument_coverage(&grant).await.unwrap();
    assert_eq!(h.service.project(&id).await.unwrap()["coverage"], "paused");
    assert_eq!(
        *h.control.calls.lock().unwrap(),
        calls,
        "Cache handoff must not open/stop/tune a worker"
    );
    let mut fresh = fixture()["sweeps"][0].clone();
    fresh["id"] = json!("grant-return-original-wide");
    fresh["interval"] =
        json!({"started_at":fixture()["clock"],"ended_at":"2026-11-11T13:00:01.123456Z"});
    *h.control.sweep_on_start.lock().unwrap() = Some((h.archive.clone(), fresh));
    h.service
        .manual_return(&id, json!({"token":token}))
        .await
        .unwrap();
    tokio::time::timeout(std::time::Duration::from_secs(3), async {
        while h.service.project(&id).await.unwrap()["coverage"] != "wide" {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    assert!(grant.check().is_err());
    assert!(h.service.confirm_instrument_coverage(&grant).await.is_err());
    assert_eq!(h.service.project(&id).await.unwrap()["coverage"], "wide");
    h.close().await;
}

#[tokio::test]
async fn missing_foreign_expired_and_cross_service_grants_have_zero_control_effects() {
    let h = Harness::new().await;
    let (_, _, target, lease) = manual(&h, "receiver").await;
    let token = lease["token"].as_str().unwrap();
    let grant = h
        .service
        .capture_instrument_control(&target, Some(token))
        .unwrap();
    let before = h.control.calls.lock().unwrap().clone();
    assert!(h.service.capture_instrument_control(&target, None).is_err());
    assert!(
        h.service
            .capture_instrument_control(&target, Some("foreign"))
            .is_err()
    );
    let other_target = ControlTarget {
        session_id: "foreign".into(),
        stream_id: target.stream_id.clone(),
    };
    assert!(
        h.service
            .capture_instrument_control(&other_target, Some(token))
            .is_err()
    );
    let foreign = Harness::new().await;
    assert!(
        foreign
            .service
            .bind_manual_worker(&grant, &worker(&h, &target))
            .is_err()
    );
    foreign.close().await;
    h.sources.advance(31);
    assert!(grant.check().is_err());
    assert!(
        h.service
            .capture_instrument_control(&target, Some(token))
            .is_err()
    );
    assert!(h.service.confirm_instrument_coverage(&grant).await.is_err());
    assert_eq!(*h.control.calls.lock().unwrap(), before);
    h.close().await;
}

#[tokio::test]
async fn original_pending_grant_wakes_on_return_and_close_without_capture_refresh() {
    let h = Harness::new().await;
    let (id, _, target, lease) = manual(&h, "receiver").await;
    let grant = h
        .service
        .capture_instrument_control(&target, lease["token"].as_str())
        .unwrap();
    let pending = tokio::spawn(async move { grant.revoked().await });
    h.service
        .manual_return(&id, json!({"token":lease["token"]}))
        .await
        .unwrap();
    tokio::time::timeout(std::time::Duration::from_secs(2), pending)
        .await
        .unwrap()
        .unwrap();
    let next = h
        .service
        .manual_acquire(&id, json!({"role":"receiver"}))
        .await
        .unwrap();
    let grant = h
        .service
        .capture_instrument_control(&target, next["token"].as_str())
        .unwrap();
    let pending = tokio::spawn(async move { grant.revoked().await });
    h.service.close().await.unwrap();
    tokio::time::timeout(std::time::Duration::from_secs(2), pending)
        .await
        .unwrap()
        .unwrap();
    h.archive.close().await.unwrap();
}

#[tokio::test]
async fn pending_manual_grant_wakes_on_expiry_without_a_policy_refresh() {
    let h = Harness::new().await;
    let (_, _, target, lease) = manual(&h, "receiver").await;
    let grant = h
        .service
        .capture_instrument_control(&target, lease["token"].as_str())
        .unwrap();
    tokio::time::pause();
    let pending = tokio::spawn(async move { grant.revoked().await });
    tokio::task::yield_now().await;
    h.sources.advance(31);
    tokio::time::advance(std::time::Duration::from_secs(31)).await;
    tokio::time::timeout(std::time::Duration::from_secs(2), pending)
        .await
        .unwrap()
        .unwrap();
    tokio::time::resume();
    h.close().await;
}

#[tokio::test]
async fn explicit_renewal_extends_a_pending_original_grant_without_recapturing() {
    let h = Harness::new().await;
    let (id, _, target, lease) = manual(&h, "receiver").await;
    let grant = h
        .service
        .capture_instrument_control(&target, lease["token"].as_str())
        .unwrap();
    tokio::time::pause();
    let pending = tokio::spawn(async move { grant.revoked().await });
    tokio::task::yield_now().await;
    h.sources.advance(20);
    tokio::time::advance(std::time::Duration::from_secs(20)).await;
    h.service
        .manual_renew(&id, json!({"token":lease["token"]}))
        .await
        .unwrap();
    tokio::task::yield_now().await;
    h.sources.advance(15);
    tokio::time::advance(std::time::Duration::from_secs(15)).await;
    assert!(
        !pending.is_finished(),
        "A legitimate renewal keeps the same original lease valid"
    );
    h.sources.advance(16);
    tokio::time::advance(std::time::Duration::from_secs(16)).await;
    tokio::time::timeout(std::time::Duration::from_secs(2), pending)
        .await
        .unwrap()
        .unwrap();
    tokio::time::resume();
    h.close().await;
}

#[tokio::test]
async fn explicitly_stopped_receiver_binds_once_without_disturbing_analyzer() {
    let h = Harness::new().await;
    let (id, armed, target, lease) = manual(&h, "receiver").await;
    let grant = h
        .service
        .capture_instrument_control(&target, lease["token"].as_str())
        .unwrap();
    let stopped = h.control.stop_stream(&target).await.unwrap();
    let mut wrong = stopped.clone();
    wrong.worker_instance = Some("foreign-instance".into());
    assert!(
        h.service
            .confirm_manual_worker_stopped(&grant, &wrong)
            .is_err()
    );
    h.service
        .confirm_manual_worker_stopped(&grant, &stopped)
        .unwrap();
    assert_eq!(grant.expected_worker_instance(), None);
    let connected = explicit_connected_worker(&h, &target, "explicit-receiver-instance");
    h.service.bind_manual_worker(&grant, &connected).unwrap();
    h.service
        .manual_renew(&id, json!({"token":lease["token"]}))
        .await
        .unwrap();
    grant.check().unwrap();
    assert!(
        h.control
            .workers
            .lock()
            .unwrap()
            .iter()
            .any(
                |worker| worker.stream_id == armed["analyzer_stream_id"].as_str().unwrap()
                    && worker.alive
            )
    );
    h.service
        .manual_return(&id, json!({"token":lease["token"]}))
        .await
        .unwrap();
    assert!(grant.check().is_err());
    h.close().await;
}

#[tokio::test]
async fn manual_wire_fields_and_errors_match_the_actual_python_oracle() {
    let python = std::env::var_os("ORI_TEST_PYTHON").unwrap_or_else(|| "python".into());
    let output = std::process::Command::new(python)
        .arg(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/tests/generate_manual_oracle.py"
        ))
        .envs([
            ("OMP_NUM_THREADS", "1"),
            ("OPENBLAS_NUM_THREADS", "1"),
            ("MKL_NUM_THREADS", "1"),
        ])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let oracle = ori_contracts::parse_json_value(&output.stdout, 65536).unwrap();
    assert_eq!(oracle["hardware_validation"], "not_run");
    let h = Harness::new().await;
    let id = h.prepared().await;
    let armed = h.service.arm(&id, json!({})).await.unwrap();
    assert_eq!(
        h.service
            .manual_acquire(&id, json!({"role":"foreign"}))
            .await
            .unwrap_err()
            .to_string(),
        oracle["errors"]["invalid_role"]
    );
    let lease = h
        .service
        .manual_acquire(&id, json!({"role":"receiver"}))
        .await
        .unwrap();
    let mut acquired = lease.clone();
    acquired["token"] = json!("__TOKEN__");
    assert_eq!(acquired, oracle["acquired"]);
    assert_eq!(
        h.service
            .manual_renew(&id, json!({"token":lease["token"]}))
            .await
            .unwrap(),
        oracle["renewed"]
    );
    assert_eq!(
        h.service
            .manual_renew(&id, json!({"token":lease["token"],"extra":1}))
            .await
            .unwrap_err()
            .to_string(),
        oracle["errors"]["invalid_renew"]
    );
    assert_eq!(
        h.service
            .manual_renew(&id, json!({"token":"foreign"}))
            .await
            .unwrap_err()
            .to_string(),
        oracle["errors"]["foreign"]
    );
    h.sources.advance(31);
    assert_eq!(
        h.service
            .manual_renew(&id, json!({"token":lease["token"]}))
            .await
            .unwrap_err()
            .to_string(),
        oracle["errors"]["expired"]
    );
    assert_eq!(
        h.service
            .manual_return(&id, json!({"token":lease["token"]}))
            .await
            .unwrap(),
        oracle["returned"]
    );
    let lease = h
        .service
        .manual_acquire(&id, json!({"role":"analyzer"}))
        .await
        .unwrap();
    let statuses = h.control.workers.lock().unwrap().clone();
    let paused = json!({"role":lease["role"],"expires_seconds":lease["expires_seconds"],
        "coverage":h.service.project(&id).await.unwrap()["coverage"],
        "analyzer_alive":statuses.iter().find(|worker| worker.stream_id == armed["analyzer_stream_id"].as_str().unwrap()).unwrap().alive,
        "receiver_alive":statuses.iter().find(|worker| worker.stream_id == armed["receiver_stream_id"].as_str().unwrap()).unwrap().alive});
    assert_eq!(paused, oracle["paused"]);
    h.close().await;
}

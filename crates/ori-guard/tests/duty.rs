mod common;
use common::*;
use ori_guard::{DutyTrigger, GuardService};
use serde_json::{Value, json};
use std::sync::Arc;

fn weekly() -> Value {
    json!({"schedule":{"mode":"weekly","timezone":"America/New_York","weekdays":[1],
        "start_local":"22:00","end_local":"02:00"},"resume_after_restart":true})
}
fn clock(f: &Harness, utc: &str) {
    *f.sources.now.lock().unwrap() = utc.parse().unwrap();
}

#[tokio::test]
async fn local_overnight_window_arms_stops_and_acknowledges_explicit_cancellation() {
    let f = Harness::new().await;
    let id = f.prepared().await;
    clock(&f, "2026-11-23T21:00:00Z");
    let waiting = f.service.arm_duty(&id, weekly()).await.unwrap();
    assert_eq!(waiting["status"], "waiting");
    assert!(!f.service.duty_acquisition_allowed(&id));
    assert!(!f.service.any_armed());
    clock(&f, "2026-11-24T03:00:00Z");
    f.service
        .reconcile_duty(&id, DutyTrigger::Timer)
        .await
        .unwrap();
    assert!(f.service.any_armed());
    assert!(f.service.duty_acquisition_allowed(&id));
    clock(&f, "2026-11-24T07:00:00Z");
    assert!(!f.service.duty_acquisition_allowed(&id));
    f.service
        .reconcile_duty(&id, DutyTrigger::Timer)
        .await
        .unwrap();
    assert!(!f.service.any_armed());
    assert_eq!(f.service.duty(&id).await.unwrap()["enabled"], true);
    f.service.disarm(&id, json!({})).await.unwrap();
    clock(&f, "2026-12-01T03:00:00Z");
    f.service
        .reconcile_duty(&id, DutyTrigger::Restart)
        .await
        .unwrap();
    assert!(!f.service.any_armed());
    assert_eq!(f.service.duty(&id).await.unwrap()["enabled"], false);
    f.close().await;
}

#[tokio::test]
async fn restart_requires_opt_in_and_wake_rechecks_preflight_before_acquisition() {
    let f = Harness::new().await;
    let id = f.prepared().await;
    let policy = json!({"schedule":{"mode":"continuous"},"resume_after_restart":false});
    assert_eq!(
        f.service.arm_duty(&id, policy).await.unwrap()["status"],
        "armed"
    );
    f.service
        .reconcile_duty(&id, DutyTrigger::Wake)
        .await
        .unwrap();
    assert!(!f.service.any_armed());
    assert_eq!(f.service.duty(&id).await.unwrap()["enabled"], false);
    let enabled = json!({"schedule":{"mode":"continuous"},"resume_after_restart":true});
    // A responsive preparation stream is still required for a fresh arm.
    *f.control.sweep_on_start.lock().unwrap() =
        Some((f.archive.clone(), fixture()["sweeps"][0].clone()));
    f.service.arm_duty(&id, enabled).await.unwrap();
    f.service.close().await.unwrap();
    *f.control.ready.lock().unwrap() = Some(false);
    let restarted = GuardService::with_sources(
        f.archive.clone(),
        f.control.clone(),
        f.sources.clone(),
        f.sources.clone(),
    );
    *f.control.service.lock().unwrap() = Arc::downgrade(&restarted);
    restarted.start().await.unwrap();
    assert!(!restarted.any_armed());
    assert_eq!(
        restarted.duty(&id).await.unwrap()["status"],
        "preflight_failed"
    );
    restarted.close().await.unwrap();
    f.archive.close().await.unwrap();
}

#[tokio::test]
async fn until_expires_at_utc_boundary_and_invalid_schedules_cannot_replace_it() {
    let f = Harness::new().await;
    let id = f.prepared().await;
    clock(&f, "2026-11-23T00:00:00Z");
    let policy = json!({"schedule":{"mode":"until","until_at":"2026-11-23T00:01:00Z"},"resume_after_restart":true});
    assert_eq!(
        f.service.arm_duty(&id, policy).await.unwrap()["status"],
        "armed"
    );
    for (key, value) in [
        ("timezone", json!("Invalid/Planet")),
        ("weekdays", json!([1, 1])),
        ("start_local", json!("25:00")),
    ] {
        let mut invalid = weekly();
        invalid["schedule"][key] = value;
        assert!(f.service.arm_duty(&id, invalid).await.is_err());
    }
    clock(&f, "2026-11-23T00:01:00Z");
    f.service
        .reconcile_duty(&id, DutyTrigger::Timer)
        .await
        .unwrap();
    assert!(!f.service.any_armed());
    assert_eq!(f.service.duty(&id).await.unwrap()["status"], "expired");
    f.close().await;
}

#[tokio::test]
async fn autumn_shift_remains_armed_between_the_two_local_one_thirties() {
    let f = Harness::new().await;
    let id = f.prepared().await;
    clock(&f, "2026-11-01T05:30:00Z");
    let p = json!({"schedule":{"mode":"weekly","timezone":"America/New_York","weekdays":[7],"start_local":"01:30","end_local":"02:00"},"resume_after_restart":true});
    assert_eq!(f.service.arm_duty(&id, p).await.unwrap()["status"], "armed");
    clock(&f, "2026-11-01T06:10:00Z");
    f.service
        .reconcile_duty(&id, DutyTrigger::Timer)
        .await
        .unwrap();
    assert!(f.service.any_armed());
    assert!(f.service.duty_acquisition_allowed(&id));
    clock(&f, "2026-11-01T07:00:00Z");
    assert!(!f.service.duty_acquisition_allowed(&id));
    f.service
        .reconcile_duty(&id, DutyTrigger::Timer)
        .await
        .unwrap();
    assert!(!f.service.any_armed());
    f.close().await;
}

#[tokio::test]
async fn status_reports_coverage_loss_while_schedule_remains_enabled() {
    let f = Harness::new().await;
    let id = f.prepared().await;
    f.service
        .arm_duty(&id, json!({"schedule":{"mode":"continuous"}}))
        .await
        .unwrap();
    f.control.workers.lock().unwrap().clear();
    f.service.tick(&id).await.unwrap();
    let duty = f.service.duty(&id).await.unwrap();
    assert_eq!(duty["enabled"], true);
    assert_eq!(duty["status"], "degraded");
    assert!(duty["last_error"].is_string());
    f.close().await;
}

#[tokio::test]
async fn failed_archive_after_sleep_cannot_keep_running_a_pre_sleep_worker() {
    let f = Harness::new().await;
    let id = f.prepared().await;
    f.service
        .arm_duty(&id, json!({"schedule":{"mode":"continuous"}}))
        .await
        .unwrap();
    let project = f.service.get_record("project", &id, None).await.unwrap();
    let (target, permit) = f.control.permits.lock().unwrap()[0].clone();
    assert_eq!(target.session_id, project["active_session_id"]);
    f.archive.close().await.unwrap();
    assert!(
        f.service
            .reconcile_duty(&id, DutyTrigger::Wake)
            .await
            .is_err()
    );
    assert!(f.service.authorize_control(&target, Some(&permit)).is_err());
    assert!(f.service.close().await.is_err());
}

#[tokio::test]
async fn a_preflight_that_finishes_after_the_window_cannot_start_instruments() {
    let f = Harness::new().await;
    let id = f.prepared().await;
    clock(&f, "2026-11-23T00:00:00Z");
    let gate = TuneGate::new();
    *f.control.preflight_gate.lock().unwrap() = Some(gate.clone());
    let owner = f.service.clone();
    let arm = tokio::spawn(async move {
        owner.arm_duty(&id,json!({
        "schedule":{"mode":"until","until_at":"2026-11-23T00:00:01Z"},"resume_after_restart":true
    })).await
    });
    gate.entered.notified().await;
    clock(&f, "2026-11-23T00:00:02Z");
    gate.release.notify_one();
    let report = arm.await.unwrap().unwrap();
    assert_eq!(report["status"], "expired");
    assert!(f.control.permits.lock().unwrap().is_empty());
    f.close().await;
}

#[tokio::test]
async fn disarm_during_preflight_prevents_a_late_worker_start() {
    let f = Harness::new().await;
    let id = f.prepared().await;
    let gate = TuneGate::new();
    *f.control.preflight_gate.lock().unwrap() = Some(gate.clone());
    let owner = f.service.clone();
    let target = id.clone();
    let arming = tokio::spawn(async move {
        owner
            .arm_duty(&target, json!({"schedule":{"mode":"continuous"}}))
            .await
    });
    gate.entered.notified().await;
    let mut stop = Box::pin(f.service.disarm(&id, json!({})));
    std::future::poll_fn(|cx| {
        assert!(std::future::Future::poll(stop.as_mut(), cx).is_pending());
        std::task::Poll::Ready(())
    })
    .await;
    gate.release.notify_one();
    arming.await.unwrap().unwrap();
    stop.await.unwrap();
    assert!(f.control.permits.lock().unwrap().is_empty());
    assert_eq!(f.service.duty(&id).await.unwrap()["enabled"], false);
    f.close().await;
}

#[tokio::test]
async fn waiting_shift_releases_collector_and_keeps_its_confirmed_originals() {
    let f = Harness::new().await;
    let id = f.prepared().await;
    let preparation = f.service.start_preparation(&id, json!({})).await.unwrap();
    let project = &preparation["project"];
    let target = ori_guard::ControlTarget {
        session_id: project["active_session_id"].as_str().unwrap().into(),
        stream_id: project["analyzer_stream_id"].as_str().unwrap().into(),
    };
    let mut originals = fixture()["sweeps"].as_array().unwrap()[..20].to_vec();
    for (index, original) in originals.iter_mut().enumerate() {
        original["id"] = json!(format!("weekly-preparation-{index}"));
        original["session_id"] = json!(target.session_id);
        original["stream_id"] = json!(target.stream_id);
    }
    f.archive.append_sweeps(originals.clone()).await.unwrap();
    let baseline = f
        .service
        .create_baseline(
            &id,
            json!({"name":"Weekly","location":"Room","confirm":true}),
        )
        .await
        .unwrap();
    assert!(f.service.authorize_control(&target, None).is_err());
    clock(&f, "2026-11-23T21:00:00Z");
    *f.control.fail_stop.lock().unwrap() = true;
    assert!(f.service.arm_duty(&id, weekly()).await.is_err());
    assert_eq!(f.service.duty(&id).await.unwrap()["status"], "stop_failed");
    assert!(f.service.authorize_control(&target, None).is_err());

    *f.control.fail_stop.lock().unwrap() = false;
    f.service
        .reconcile_duty(&id, DutyTrigger::Timer)
        .await
        .unwrap();
    assert_eq!(f.service.duty(&id).await.unwrap()["status"], "waiting");
    assert!(f.service.authorize_control(&target, None).is_ok());
    assert!(
        f.control
            .workers
            .lock()
            .unwrap()
            .iter()
            .filter(|w| w.session_id == target.session_id)
            .all(|w| !w.alive)
    );
    assert_eq!(
        f.service.project(&id).await.unwrap()["baseline_id"],
        baseline["id"]
    );
    assert_eq!(
        f.archive.get_sweep("weekly-preparation-0").await.unwrap(),
        Some(originals[0].clone())
    );
    assert!(f.service.start_preparation(&id, json!({})).await.is_err());
    f.close().await;
}

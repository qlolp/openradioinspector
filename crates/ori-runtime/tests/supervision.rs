#![cfg(unix)]
use ori_runtime::supervisor::*;
use std::{
    collections::BTreeMap,
    net::{SocketAddr, TcpListener},
    path::Path,
};
fn spec(data: &Path, mode: &str, address: SocketAddr) -> BackendSpec {
    BackendSpec {
        name: mode.into(),
        program: "/usr/bin/python3".into(),
        args: vec![
            format!("{}/tests/fake_backend.py", env!("CARGO_MANIFEST_DIR")),
            data.display().to_string(),
            address.port().to_string(),
            mode.into(),
        ],
        environment: BTreeMap::new(),
        control_addr: address,
        ports: vec![address],
        startup_timeout_ms: 2000,
        shutdown_timeout_ms: 2000,
    }
}
fn address() -> SocketAddr {
    TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
}
fn options() -> CutoverOptions {
    CutoverOptions {
        automatic: false,
        acknowledge_coverage_gap: true,
    }
}

#[tokio::test]
async fn failed_candidate_is_fully_stopped_before_rollback_preserves_latest_data() {
    let temp = tempfile::tempdir().unwrap();
    let addr = address();
    let mut supervisor = Supervisor::new(temp.path()).unwrap();
    supervisor
        .start(spec(temp.path(), "legacy", addr))
        .await
        .unwrap();
    let result = supervisor
        .cutover(spec(temp.path(), "crash", addr), options())
        .await
        .unwrap();
    assert!(matches!(result, CutoverOutcome::RolledBack { .. }));
    assert!(supervisor.status().await.unwrap().ready);
    supervisor.shutdown().await.unwrap();
    assert_eq!(
        std::fs::read_to_string(temp.path().join("committed-data")).unwrap(),
        "legacy\ncrash\nlegacy\n"
    );
    let journal =
        std::fs::read_to_string(temp.path().join(".ori-runtime/coverage.ndjson")).unwrap();
    assert!(journal.contains("coverage_interruption_started"));
    assert!(journal.contains("coverage_interruption_ended"));
}

#[tokio::test]
async fn armed_automatic_cutover_is_refused_explicit_ack_preserves_resume_intent() {
    let temp = tempfile::tempdir().unwrap();
    let addr = address();
    let mut supervisor = Supervisor::new(temp.path()).unwrap();
    supervisor
        .start(spec(temp.path(), "armed", addr))
        .await
        .unwrap();
    assert!(
        supervisor
            .cutover(
                spec(temp.path(), "candidate", addr),
                CutoverOptions {
                    automatic: true,
                    acknowledge_coverage_gap: true
                }
            )
            .await
            .is_err()
    );
    assert!(supervisor.status().await.unwrap().guard_armed);
    assert!(
        supervisor
            .cutover(
                spec(temp.path(), "candidate", addr),
                CutoverOptions {
                    automatic: false,
                    acknowledge_coverage_gap: false
                }
            )
            .await
            .is_err()
    );
    assert!(matches!(
        supervisor
            .cutover(spec(temp.path(), "candidate", addr), options())
            .await
            .unwrap(),
        CutoverOutcome::Activated
    ));
    let intent: serde_json::Value =
        serde_json::from_slice(&std::fs::read(temp.path().join("shutdown-intent.json")).unwrap())
            .unwrap();
    assert_eq!(intent["preserve_resume_requested"], true);
    assert_eq!(intent["acknowledge_coverage_gap"], true);
    supervisor.shutdown().await.unwrap();
}

#[tokio::test]
async fn occupied_legacy_port_and_foreign_writer_require_orderly_handover() {
    let temp = tempfile::tempdir().unwrap();
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let mut supervisor = Supervisor::new(temp.path()).unwrap();
    assert!(
        supervisor
            .start(spec(
                temp.path(),
                "candidate",
                listener.local_addr().unwrap()
            ))
            .await
            .is_err()
    );
    assert!(listener.local_addr().is_ok());
    drop(listener);
    let _writer = ori_runtime::lease::WriterLease::acquire(temp.path()).unwrap();
    assert!(
        supervisor
            .start(spec(temp.path(), "candidate", address()))
            .await
            .is_err()
    );
    assert!(!temp.path().join("committed-data").exists());
}

#[tokio::test]
async fn readiness_requires_actual_writer_lease_and_failed_start_releases_owned_process() {
    let temp = tempfile::tempdir().unwrap();
    let addr = address();
    let mut supervisor = Supervisor::new(temp.path()).unwrap();
    let mut liar = spec(temp.path(), "no-writer", addr);
    liar.startup_timeout_ms = 200;
    assert!(
        supervisor.start(liar).await.is_err(),
        "readiness trusted a flag without an actual writer lease"
    );
    supervisor
        .start(spec(temp.path(), "legacy", addr))
        .await
        .unwrap();
    supervisor.shutdown().await.unwrap();
}

#[tokio::test]
async fn incomplete_startup_preflight_fails_and_does_not_leave_a_writer() {
    let temp = tempfile::tempdir().unwrap();
    let addr = address();
    let mut supervisor = Supervisor::new(temp.path()).unwrap();
    let mut candidate = spec(temp.path(), "not-ready", addr);
    candidate.startup_timeout_ms = 200;
    assert!(supervisor.start(candidate).await.is_err());
    supervisor
        .start(spec(temp.path(), "legacy", addr))
        .await
        .unwrap();
    supervisor.shutdown().await.unwrap();
}

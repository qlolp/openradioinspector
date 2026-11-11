#![cfg(target_os = "linux")]
use ori_runtime::worker::*;
use std::collections::BTreeMap;
use std::time::Duration;
fn spec() -> WorkerSpec {
    WorkerSpec {
        program: "/usr/bin/python3".into(),
        args: vec![format!(
            "{}/tests/fake_worker.py",
            env!("CARGO_MANIFEST_DIR")
        )],
        environment: BTreeMap::new(),
        deadline_ms: 2000,
        require_hard_memory: false,
        linux_cgroup: None,
    }
}
fn request(operation: &str) -> Vec<u8> {
    serde_json::to_vec(&serde_json::json!({"version":1,"request_id":"job-1","operation":operation}))
        .unwrap()
}
#[tokio::test]
async fn worker_gets_bounded_limits_and_reports_best_effort_enforcement_honestly() {
    let result = ResearchPool::default()
        .run(spec(), request("summary"), Cancellation::default())
        .await
        .unwrap();
    assert_eq!(
        result.response["result"]["duckdb_memory_bytes"],
        536870912u64
    );
    assert_eq!(result.response["result"]["worker_rss_bytes"], 805306368u64);
    assert_eq!(result.response["result"]["duckdb_threads"], 2);
    assert_eq!(result.response["result"]["cache_bytes"], 10737418240u64);
    assert_eq!(result.response["result"]["temp_bytes"], 10737418240u64);
    assert_eq!(
        result.response["result"]["io_bytes_per_second"],
        20971520u64
    );
    assert!(!result.enforcement.hard_memory);
    assert!(!result.enforcement.hard_io);
    assert!(result.enforcement.process_tree_termination);
    let mut strict = spec();
    strict.require_hard_memory = true;
    assert!(
        ResearchPool::default()
            .run(strict, request("summary"), Cancellation::default())
            .await
            .is_err()
    );
}
#[tokio::test]
async fn heavy_admission_rejects_overload_and_cancellation_releases_slot() {
    let pool = std::sync::Arc::new(ResearchPool::default());
    let cancel = Cancellation::default();
    let running = {
        let pool = pool.clone();
        let cancel = cancel.clone();
        tokio::spawn(async move { pool.run(spec(), request("sleep"), cancel).await })
    };
    tokio::time::sleep(Duration::from_millis(50)).await;
    assert!(
        pool.run(spec(), request("summary"), Cancellation::default())
            .await
            .is_err(),
        "second heavy job admitted"
    );
    cancel.cancel();
    assert!(running.await.unwrap().is_err());
    pool.run(spec(), request("summary"), Cancellation::default())
        .await
        .unwrap();
}
#[tokio::test]
async fn deadline_terminates_owned_descendants_and_oversize_output_is_bounded() {
    let temp = tempfile::tempdir().unwrap();
    let pidfile = temp.path().join("descendant");
    let mut config = spec();
    config.deadline_ms = 200;
    let body=serde_json::to_vec(&serde_json::json!({"version":1,"request_id":"job-2","operation":"fork","pid_file":pidfile})).unwrap();
    assert!(
        ResearchPool::default()
            .run(config, body, Cancellation::default())
            .await
            .is_err()
    );
    let pid: usize = std::fs::read_to_string(pidfile).unwrap().parse().unwrap();
    if let Ok(stat) = std::fs::read_to_string(format!("/proc/{pid}/stat")) {
        assert_eq!(stat.split(") ").nth(1).unwrap().chars().next(), Some('Z'));
    }
    assert!(
        ResearchPool::default()
            .run(spec(), request("huge"), Cancellation::default())
            .await
            .is_err()
    );
}

#[tokio::test]
async fn duplicate_json_fields_are_rejected_before_the_child_can_normalize_them() {
    let request =
        br#"{"version":1,"request_id":"job-1","operation":"summary","operation":"summary"}"#
            .to_vec();
    assert!(
        ResearchPool::default()
            .run(spec(), request, Cancellation::default())
            .await
            .is_err()
    );
}

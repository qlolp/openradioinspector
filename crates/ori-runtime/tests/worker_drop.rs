#![cfg(target_os = "linux")]
use ori_runtime::{
    Error,
    worker::{Cancellation, ResearchPool, WorkerSpec},
};
use std::{collections::BTreeMap, path::PathBuf, time::Duration};

struct Release(PathBuf);
impl Drop for Release {
    fn drop(&mut self) {
        let _ = std::fs::write(&self.0, b"release");
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn dropped_waiter_retains_admission_until_inherited_ipc_pipes_are_really_joined() {
    let root = tempfile::tempdir().unwrap();
    let ready = root.path().join("ready");
    let release = root.path().join("release");
    let _release_on_failure = Release(release.clone());
    let pool = ResearchPool::default();
    let worker = WorkerSpec {
        program: "/usr/bin/python3".into(),
        args: vec![format!(
            "{}/tests/fake_worker.py",
            env!("CARGO_MANIFEST_DIR")
        )],
        environment: BTreeMap::new(),
        deadline_ms: 2000,
        require_hard_memory: false,
        linux_cgroup: None,
    };
    let body = serde_json::to_vec(&serde_json::json!({"version":1,"request_id":"owned-pipes",
        "operation":"hold_pipes","ready_file":ready,"release_file":release}))
    .unwrap();
    let running = {
        let pool = pool.clone();
        tokio::spawn(async move { pool.run(worker, body, Cancellation::default()).await })
    };
    tokio::time::timeout(Duration::from_secs(2), async {
        while !ready.exists() {
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .unwrap();
    running.abort();
    let _ = running.await;
    tokio::time::sleep(Duration::from_millis(50)).await;
    assert!(
        matches!(pool.try_reserve(), Err(Error::Busy)),
        "admission escaped while a worker IPC reader was still blocked"
    );
    std::fs::write(&release, b"release").unwrap();
    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            match pool.try_reserve() {
                Ok(permit) => {
                    drop(permit);
                    break;
                }
                Err(Error::Busy) => tokio::time::sleep(Duration::from_millis(5)).await,
                Err(error) => panic!("unexpected admission failure: {error}"),
            }
        }
    })
    .await
    .unwrap();
}

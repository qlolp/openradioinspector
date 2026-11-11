use ori_runtime::{
    Error,
    worker::{Cancellation, ResearchPool, WorkerSpec},
};
use std::collections::BTreeMap;

#[test]
fn preparation_reservation_is_global_across_pool_clones_and_released_on_drop() {
    let pool = ResearchPool::default();
    let other = pool.clone();
    let preparation = pool.try_reserve().unwrap();
    assert!(matches!(other.try_reserve(), Err(Error::Busy)));
    drop(preparation);
    let next = other.try_reserve().unwrap();
    assert!(matches!(pool.try_reserve(), Err(Error::Busy)));
    drop(next);
    assert!(pool.try_reserve().is_ok());
}

#[tokio::test]
async fn the_reserved_job_retains_admission_until_cancelled_before_launch() {
    let pool = ResearchPool::default();
    let preparation = pool.try_reserve().unwrap();
    let invalid = || WorkerSpec {
        program: "not-an-absolute-or-existing-program".into(),
        args: vec![],
        environment: BTreeMap::new(),
        deadline_ms: 1,
        require_hard_memory: true,
        linux_cgroup: None,
    };
    assert!(matches!(
        pool.run(invalid(), vec![], Cancellation::default()).await,
        Err(Error::Busy)
    ));
    let cancelled = Cancellation::default();
    cancelled.cancel();
    assert!(matches!(
        preparation.run(invalid(), vec![], cancelled).await,
        Err(Error::Cancelled)
    ));
    assert!(pool.try_reserve().is_ok());
}

#[cfg(target_os = "linux")]
#[tokio::test]
async fn retained_run_keeps_original_reservation_after_success_and_failure_for_owner_cleanup() {
    let pool = ResearchPool::default();
    let spec = WorkerSpec {
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
    let body = serde_json::to_vec(
        &serde_json::json!({"version":1,"request_id":"retained","operation":"summary"}),
    )
    .unwrap();
    let (reservation, result) = pool
        .try_reserve()
        .unwrap()
        .run_retained(spec.clone(), body.clone(), Cancellation::default())
        .await;
    assert!(result.is_ok());
    assert!(
        matches!(pool.try_reserve(), Err(Error::Busy)),
        "completed child released admission before owner cleanup"
    );
    drop(reservation);
    let cancelled = Cancellation::default();
    cancelled.cancel();
    let (reservation, result) = pool
        .try_reserve()
        .unwrap()
        .run_retained(spec, body, cancelled)
        .await;
    assert!(matches!(result, Err(Error::Cancelled)));
    assert!(
        matches!(pool.try_reserve(), Err(Error::Busy)),
        "failed child released admission before owner cleanup"
    );
    drop(reservation);
    assert!(pool.try_reserve().is_ok());
}

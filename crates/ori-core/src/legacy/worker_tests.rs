//! Original reservation/file-I/O hook composition, using owned temporary state.
use super::*;
use std::sync::Arc;
fn spec(marker: &std::path::Path) -> WorkerSpec {
    WorkerSpec {
        program: std::env::var_os("ORI_TEST_PYTHON")
            .map(Into::into)
            .unwrap_or_else(|| "/usr/bin/python3".into()),
        args: vec![
            format!(
                "{}/tests/private_worker_echo.py",
                env!("CARGO_MANIFEST_DIR")
            ),
            marker.to_str().unwrap().into(),
            "success".into(),
        ],
        environment: std::collections::BTreeMap::new(),
        deadline_ms: 10_000,
        require_hard_memory: false,
        linux_cgroup: None,
    }
}
#[tokio::test]
async fn legacy_original_io_hook_retains_heavy_admission_after_the_permit_owner_drops() {
    let data = tempfile::tempdir().unwrap();
    let mut config = crate::CoreConfig::replay(data.path());
    config.legacy.worker = Some(spec(&data.path().join("unused.pid")));
    let core = Core::open(config).await.unwrap();
    let permit = Science(Arc::downgrade(&core)).try_reserve().unwrap();
    let budget = permit.io_budget();
    let supplied = budget.is_some();
    drop(permit);
    let held = matches!(
        core.research_pool.try_reserve(),
        Err(ori_runtime::Error::Busy)
    );
    drop(budget);
    let released = core.research_pool.try_reserve().is_ok();
    core.close().await.unwrap();
    assert!(
        supplied && held && released,
        "Legacy preparation/post-write has no original shared admission budget"
    );
}
#[tokio::test]
async fn legacy_file_charges_share_qos_and_post_worker_owner_holds_the_original_reservation() {
    let data = tempfile::tempdir().unwrap();
    let mut config = crate::CoreConfig::replay(data.path());
    config.legacy.worker = Some(spec(&data.path().join("owned.pid")));
    let core = Core::open(config).await.unwrap();
    let permit = Science(Arc::downgrade(&core)).try_reserve().unwrap();
    let Some(budget) = permit.io_budget() else {
        drop(permit);
        core.close().await.unwrap();
        panic!("Legacy file accounting hook is missing");
    };
    let before = core.background.as_ref().unwrap().qos.status().logical_bytes;
    let owned = budget.clone();
    tokio::task::spawn_blocking(move || owned.account(1024, &LegacyCancellation::default()))
        .await
        .unwrap()
        .unwrap();
    let after = core.background.as_ref().unwrap().qos.status().logical_bytes;
    let response = permit
        .execute(
            serde_json::json!({"version":1,"request_id":"legacy-original","operation":"analysis"}),
            LegacyCancellation::default(),
        )
        .await;
    let held = matches!(
        core.research_pool.try_reserve(),
        Err(ori_runtime::Error::Busy)
    );
    let registered = core
        .background
        .as_ref()
        .unwrap()
        .qos
        .status()
        .registered_work;
    drop(budget);
    let released = core.research_pool.try_reserve().is_ok();
    core.close().await.unwrap();
    assert_eq!(after - before, 1024);
    assert_eq!(registered, 1);
    assert!(response.is_ok() && held && released);
}

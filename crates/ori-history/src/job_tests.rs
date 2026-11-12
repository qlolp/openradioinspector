//! Pin the two existing RSS error origins without forcing an allocation failure.
use super::*;
use ori_archive::ArchiveConfig;
use ori_runtime::worker::ResearchPool;
use std::{collections::BTreeMap, path::PathBuf};

fn synthetic_worker(script: &str) -> WorkerSpec {
    WorkerSpec {
        program: std::env::var_os("ORI_RESEARCH_PYTHON")
            .map(PathBuf::from)
            .expect("Set ORI_RESEARCH_PYTHON to the installed isolated interpreter"),
        args: vec!["-I".into(), "-B".into(), "-c".into(), script.into()],
        environment: BTreeMap::new(),
        deadline_ms: 10_000,
        require_hard_memory: false,
        linux_cgroup: None,
    }
}

#[tokio::test]
async fn worker_reported_rss_error_is_preserved_after_owned_child_join() {
    // The child reports a cooperative failure under the normal parent budget.
    // This exercises real IPC/exit-2 handling, not actual RSS enforcement.
    let worker = synthetic_worker(
        r#"
import json, sys
request = json.load(sys.stdin)
print(json.dumps({'version': 1, 'request_id': request['request_id'], 'ok': False,
    'error': {'code': 'rss_limit_exceeded', 'message': 'Worker memory budget exceeded',
              'retryable': True}, 'metrics': {}}), flush=True)
sys.exit(2)
"#,
    );
    let root = tempfile::tempdir_in(std::env::temp_dir().canonicalize().unwrap()).unwrap();
    let archive = Archive::open(ArchiveConfig::new(root.path().join("source.sqlite"))).unwrap();
    let fixture: Value = serde_json::from_str(include_str!(
        "../../../contracts/fixtures/measurement-v1.json"
    ))
    .unwrap();
    let mut session = fixture["session"].clone();
    session["created_at"] = json!("2026-11-11T12:00:00.123456Z");
    archive.create_session(session).await.unwrap();
    archive
        .append_sweeps(vec![fixture["sweep"].clone()])
        .await
        .unwrap();
    let original = archive.get_sweep("fixture-sweep-α").await.unwrap();
    let config = HistoryConfig::new(root.path().join("cache"), root.path().join("snapshots"));
    let roots = Arc::new(Roots::open(&config).unwrap());
    let pool = ResearchPool::default();
    let mut request = HistoryRequest::new(Operation::Export);
    request.filters.session_id = Some("fixture-сессия".into());
    // Use the private job boundary; the production service still requires its
    // exact installed-worker arguments and never accepts synthetic scripts.
    let record = run(JobWork {
        archive: archive.clone(),
        roots: roots.clone(),
        config,
        worker,
        request,
        replay: None,
        permit: Some(pool.try_reserve().unwrap()),
        cancel: Cancellation::default(),
        current: Arc::new(Mutex::new(JobView::new(
            "0123456789abcdef0123456789abcdef".into(),
            Operation::Export,
        ))),
    })
    .await;
    let view = record.view.clone();
    let output_absent = record.output.is_none();
    let source_retired = record.snapshot.is_none();
    let permit_retained = matches!(pool.try_reserve(), Err(ori_runtime::Error::Busy));
    drop(record);
    let pool_reusable = pool.try_reserve().is_ok();
    let cleanup_failed = roots.cleanup_failed();
    let after = archive.get_sweep("fixture-sweep-α").await;
    let closed = archive.close().await;
    drop(roots);

    // All child/IPC work and archive closure finish before outcome assertions.
    closed.unwrap();
    assert!(original.is_some());
    assert_eq!(after.unwrap(), original);
    assert!(!cleanup_failed);
    assert!(
        permit_retained,
        "job released its retained reservation early"
    );
    assert!(
        pool_reusable,
        "failed child did not release its reservation"
    );
    assert!(output_absent && source_retired);
    assert_eq!(view.phase, JobPhase::Failed);
    assert!(!view.result_available);
    let failure = view.failure.unwrap();
    assert_eq!(failure.code, "rss_limit_exceeded");
    assert!(failure.retryable);
}

#[cfg(target_os = "linux")]
#[tokio::test]
async fn parent_sampled_rss_error_maps_to_history_resource_limit() {
    // The inert child never reports a worker error. Linux's native RSS sampler
    // must produce ResourceLimit; deadline, crash and protocol errors cannot pass.
    // Its watchdog is shorter than the unchanged 10 s worker deadline.
    let worker = synthetic_worker("import json, sys, time; json.load(sys.stdin); time.sleep(5)");
    let pool = ResearchPool::default();
    let request = serde_json::to_vec(&json!({
        "version": 1, "request_id": "parent-rss-origin", "operation": "export",
        "limits": {"worker_rss_bytes": 1}
    }))
    .unwrap();
    let result = pool.run(worker, request, Cancellation::default()).await;
    let pool_reusable = pool.try_reserve().is_ok();

    // ResearchPool::run returns only after the real child and IPC threads join.
    assert!(pool_reusable, "parent-killed child was not reaped");
    let error = match result {
        Err(ori_runtime::Error::ResourceLimit) => ori_runtime::Error::ResourceLimit,
        Err(error) => panic!("expected native parent RSS rejection, got {error:?}"),
        Ok(_) => panic!("native parent RSS rejection did not occur"),
    };
    let failure = HistoryError::from(error).failure();
    assert_eq!(failure.code, "resource_limit");
    assert!(failure.retryable);
}
